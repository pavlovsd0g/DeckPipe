//! Windows current-user IPC. Not a boundary against compromised same-user code.
use std::{
    ffi::c_void,
    hash::{Hash, Hasher},
    os::windows::io::AsRawHandle,
    path::Path,
};
use tokio::net::windows::named_pipe::{
    ClientOptions, NamedPipeClient, NamedPipeServer, ServerOptions,
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, LocalFree},
    Security::Authorization::{
        ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
    },
    Security::{GetTokenInformation, TokenUser, SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER},
    System::Pipes::{GetNamedPipeClientProcessId, GetNamedPipeServerProcessId},
    System::RemoteDesktop::ProcessIdToSessionId,
    System::Threading::{
        GetCurrentProcess, OpenProcess, OpenProcessToken, QueryFullProcessImageNameW,
        PROCESS_QUERY_LIMITED_INFORMATION,
    },
};

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

fn user_sid() -> Result<String, &'static str> {
    unsafe {
        let mut token = std::ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return Err("AUTH_USER_UNAVAILABLE");
        }
        let mut size = 0;
        GetTokenInformation(token, TokenUser, std::ptr::null_mut(), 0, &mut size);
        let mut buffer = vec![
            0usize;
            (size as usize + std::mem::size_of::<usize>() - 1)
                / std::mem::size_of::<usize>()
        ];
        let ok = GetTokenInformation(
            token,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            size,
            &mut size,
        );
        CloseHandle(token);
        if ok == 0 {
            return Err("AUTH_USER_UNAVAILABLE");
        }
        let user = &*(buffer.as_ptr() as *const TOKEN_USER);
        let mut sid = std::ptr::null_mut();
        if ConvertSidToStringSidW(user.User.Sid, &mut sid) == 0 {
            return Err("AUTH_USER_UNAVAILABLE");
        }
        let mut len = 0;
        while *sid.add(len) != 0 {
            len += 1;
        }
        let text = String::from_utf16_lossy(std::slice::from_raw_parts(sid, len));
        LocalFree(sid.cast());
        Ok(text)
    }
}

pub fn pipe_name() -> Result<String, &'static str> {
    let sid = user_sid()?;
    let mut session = 0;
    if unsafe { ProcessIdToSessionId(std::process::id(), &mut session) } == 0 {
        return Err("AUTH_USER_UNAVAILABLE");
    }
    let exe = std::env::current_exe().map_err(|_| "AUTH_HOST_UNAVAILABLE")?;
    let directory = exe
        .parent()
        .ok_or("AUTH_HOST_UNAVAILABLE")?
        .canonicalize()
        .map_err(|_| "AUTH_HOST_UNAVAILABLE")?;
    let mut hash = std::collections::hash_map::DefaultHasher::new();
    directory.to_string_lossy().to_lowercase().hash(&mut hash);
    Ok(format!(
        r"\\.\pipe\DeckPipe.Auth.v1.{}.{}.{:016x}",
        sid,
        session,
        hash.finish()
    ))
}

pub fn create_server(name: &str, first: bool) -> Result<NamedPipeServer, &'static str> {
    unsafe {
        let descriptor = wide(&format!("D:P(A;;GA;;;{})", user_sid()?));
        let mut security = std::ptr::null_mut();
        if ConvertStringSecurityDescriptorToSecurityDescriptorW(
            descriptor.as_ptr(),
            1,
            &mut security,
            std::ptr::null_mut(),
        ) == 0
        {
            return Err("AUTH_PIPE_SECURITY");
        }
        let mut attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: security,
            bInheritHandle: 0,
        };
        let result = ServerOptions::new()
            .first_pipe_instance(first)
            .reject_remote_clients(true)
            .create_with_security_attributes_raw(
                name,
                (&mut attributes as *mut SECURITY_ATTRIBUTES).cast::<c_void>(),
            );
        LocalFree(security);
        result.map_err(|_| "AUTH_PIPE_UNAVAILABLE")
    }
}

fn same_image(pid: u32, expected: &Path) -> Result<(), &'static str> {
    unsafe {
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return Err("AUTH_PEER_REJECTED");
        }
        let mut name = vec![0u16; 32768];
        let mut len = name.len() as u32;
        let ok = QueryFullProcessImageNameW(process, 0, name.as_mut_ptr(), &mut len);
        CloseHandle(process);
        if ok == 0 {
            return Err("AUTH_PEER_REJECTED");
        }
        let actual = String::from_utf16_lossy(&name[..len as usize]);
        let expected = expected
            .canonicalize()
            .map_err(|_| "AUTH_PEER_REJECTED")?
            .to_string_lossy()
            .to_string();
        if actual.trim_start_matches(r"\\?\").to_lowercase()
            != expected.trim_start_matches(r"\\?\").to_lowercase()
        {
            return Err("AUTH_PEER_REJECTED");
        }
        Ok(())
    }
}

pub fn verify_client(server: &NamedPipeServer, expected: &Path) -> Result<(), &'static str> {
    let mut pid = 0;
    if unsafe { GetNamedPipeClientProcessId(server.as_raw_handle() as _, &mut pid) } == 0 {
        return Err("AUTH_PEER_REJECTED");
    }
    same_image(pid, expected)
}

pub async fn connect_client(name: &str, expected: &Path) -> Result<NamedPipeClient, &'static str> {
    let pipe = ClientOptions::new()
        .open(name)
        .map_err(|_| "DECKPIPE_NOT_RUNNING")?;
    let mut pid = 0;
    if unsafe { GetNamedPipeServerProcessId(pipe.as_raw_handle() as _, &mut pid) } == 0 {
        return Err("AUTH_PEER_REJECTED");
    }
    same_image(pid, expected)?;
    Ok(pipe)
}
