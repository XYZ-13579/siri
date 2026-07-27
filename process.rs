


use std::net::TcpStream;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use reqwest::blocking::Client;
use tauri::{AppHandle, Manager};

const CREATE_NO_WINDOW: u32 = 0x08000000;

// 監視ループの間隔（秒）
const WATCH_INTERVAL_SECS: u64 = 10;

// llama-server の起動を待つ最大試行回数（500 ms × 120 = 60 秒）
const LLAMA_READY_POLL_MS: u64 = 500;
const LLAMA_READY_MAX_ATTEMPTS: u32 = 120;

// 探索対象とするファイル名（bin_dir 特定の目印）
const LISTENER_EXE_NAME: &str = "listener.exe";

// リソース一式（llama / model / vosk-model）が揃っているかどうかのマーカー
const RESOURCE_MARKER_DIR: &str = "llama";
const MAX_SEARCH_DEPTH: u32 = 4;

#[derive(Clone, Debug)]
pub struct ResourcePaths {
    base: PathBuf,
    bin: PathBuf,
}

impl ResourcePaths {
    pub fn resolve(app: &AppHandle) -> Result<Self, String> {
        let roots = Self::collect_search_roots(app)?;

        println!("[Manager] Searching for resources starting from {} root candidate(s):", roots.len());
        for r in &roots {
            println!("[Manager]   Root: {}", r.display());
        }

        let base = Self::find_dir_containing_entry(&roots, RESOURCE_MARKER_DIR, MAX_SEARCH_DEPTH);
        let bin = Self::find_dir_containing_entry(&roots, LISTENER_EXE_NAME, MAX_SEARCH_DEPTH);

        let base = match base {
            Some(dir) => {
                println!("[Manager] Found (base, marker='{}'): {}", RESOURCE_MARKER_DIR, dir.display());
                dir
            }
            None => {
                return Err(format!(
                    "Resource base directory was not found (marker '{}' missing). Searched roots: [{}]",
                    RESOURCE_MARKER_DIR,
                    roots.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(", ")
                ));
            }
        };

        let bin = match bin {
            Some(dir) => {
                println!("[Manager] Found (bin, marker='{}'): {}", LISTENER_EXE_NAME, dir.display());
                dir
            }
            None => {
                return Err(format!(
                    "{} was not found. Searched roots: [{}]",
                    LISTENER_EXE_NAME,
                    roots.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(", ")
                ));
            }
        };

        Ok(ResourcePaths { base, bin })
    }

    fn collect_search_roots(app: &AppHandle) -> Result<Vec<PathBuf>, String> {
        let mut roots: Vec<PathBuf> = Vec::new();

        if let Ok(resource_dir) = app.path().resource_dir() {
            roots.push(resource_dir);
        } else {
            println!("[Manager] resource_dir() could not be resolved (this is normal in some dev setups).");
        }

        if let Ok(exe_path) = std::env::current_exe() {
            if let Some(exe_dir) = exe_path.parent() {
                roots.push(exe_dir.to_path_buf());
            }
        } else {
            println!("[Manager] current_exe() could not be resolved.");
        }

        if roots.is_empty() {
            return Err("Neither resource_dir() nor current_exe() could be resolved.".to_string());
        }

        roots.sort();
        roots.dedup();
        Ok(roots)
    }

    fn find_dir_containing_entry(roots: &[PathBuf], target_name: &str, max_depth: u32) -> Option<PathBuf> {
        for root in roots {
            if let Some(found) = Self::find_dir_containing(root, target_name, max_depth) {
                return Some(found);
            }
        }
        None
    }

    fn find_dir_containing(root: &Path, target_name: &str, max_depth: u32) -> Option<PathBuf> {
        let mut queue: std::collections::VecDeque<(PathBuf, u32)> = std::collections::VecDeque::new();
        queue.push_back((root.to_path_buf(), 0));

        while let Some((dir, depth)) = queue.pop_front() {
            if !dir.is_dir() { continue; }

            let candidate = dir.join(target_name);
            if candidate.exists() { return Some(dir); }
            if depth >= max_depth { continue; }

            let entries = match std::fs::read_dir(&dir) {
                Ok(e) => e,
                Err(_) => continue,
            };

            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() { queue.push_back((path, depth + 1)); }
            }
        }
        None
    }

    pub fn listener_exe(&self) -> PathBuf {
        self.bin.join(LISTENER_EXE_NAME)
    }

    pub fn bin_dir(&self) -> &Path {
        &self.bin
    }

    pub fn llama_server_exe(&self) -> PathBuf {
        self.base.join("llama").join("llama-server.exe")
    }

    pub fn llama_model_file(&self) -> PathBuf {
        self.base.join("model").join("LFM2.5-1.2B-Instruct-Q8_0.gguf")
    }

    #[allow(dead_code)]
    pub fn vosk_model_dir(&self) -> PathBuf {
        self.base.join("vosk-model")
    }
}

pub struct ProcessManager {
    llama_server: Option<Child>,
    listener: Option<Child>,
    paths: ResourcePaths,
}

impl ProcessManager {
    pub fn new(paths: ResourcePaths) -> Self {
        ProcessManager { llama_server: None, listener: None, paths }
    }

    /// ポート8000が使用中（TCP接続可能）かチェックする
    fn is_port_in_use(port: u16) -> bool {
        TcpStream::connect(("127.0.0.1", port)).is_ok()
    }

    /// llama-server が正常に起動しているか確認する
    /// ポート接続成功を第一条件とし、HTTP応答を第二条件とする
    fn is_llama_server_ready() -> bool {
        if Self::is_port_in_use(8000) {
            let client = Client::new();
            return client
                .get("http://127.0.0.1:8000/v1/models")
                .timeout(Duration::from_millis(500))
                .send()
                .map(|r| r.status().is_success())
                .unwrap_or(false);
        }
        false
    }

    fn spawn_llama_server(&self) -> Option<Child> {
        let exe_path = self.paths.llama_server_exe();
        let model_path = self.paths.llama_model_file();

        // 1. ファイル存在チェック（厳密化）
        if !exe_path.exists() {
            eprintln!("[Manager-Error] llama-server.exe not found at: {}", exe_path.display());
            return None;
        }
        if !model_path.exists() {
            eprintln!("[Manager-Error] Model file not found at: {}", model_path.display());
            return None;
        }

        // 2. ポート競合チェック
        if Self::is_port_in_use(8000) {
            eprintln!("[Manager-Warning] Port 8000 is already in use. Skipping llama-server launch.");
            return None;
        }

        // 3. cwd の厳密な設定（親ディレクトリを確実にとる）
        let cwd = exe_path.parent().expect("[Manager-Error] Failed to get parent directory of llama-server.exe");

        // 4. 起動前ログの出力
        println!("[Manager] Preparing to spawn llama-server:");
        println!("[Manager]   EXE Path:   {}", exe_path.display());
        println!("[Manager]   Model Path: {}", model_path.display());
        println!("[Manager]   CWD:        {}", cwd.display());

        // 5. プロセス起動 (stdout/stderr を inherit)
        match Command::new(&exe_path)
            .current_dir(cwd)
            .args(&["-m", model_path.to_string_lossy().as_ref(), "--port", "8000"])
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
        {
            Ok(child) => {
                println!("[Manager] llama-server spawned successfully (PID={}).", child.id());
                Some(child)
            }
            Err(e) => {
                // エラー時の詳細出力
                eprintln!("[Manager-Error] Failed to start llama-server.exe.");
                eprintln!("[Manager-Error]   Reason:     {}", e);
                eprintln!("[Manager-Error]   EXE Path:   {}", exe_path.display());
                eprintln!("[Manager-Error]   Model Path: {}", model_path.display());
                eprintln!("[Manager-Error]   CWD:        {}", cwd.display());
                None
            }
        }
    }

    fn is_listener_running() -> bool {
        let output = Command::new("tasklist")
            .args(&["/FI", "IMAGENAME eq listener.exe", "/NH"])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
        match output {
            Ok(o) => {
                let stdout = String::from_utf8_lossy(&o.stdout);
                stdout.contains("listener.exe")
            }
            Err(_) => false,
        }
    }

    fn spawn_listener(&self) -> Option<Child> {
        let exe_path = self.paths.listener_exe();

        if !exe_path.exists() {
            eprintln!("[Manager-Error] listener.exe not found at: {}", exe_path.display());
            return None;
        }

        println!("[Manager] Starting listener.exe at {}...", exe_path.display());
        match Command::new(&exe_path)
            .current_dir(self.paths.bin_dir())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
        {
            Ok(child) => {
                println!("[Manager] listener spawned (PID={}).", child.id());
                Some(child)
            }
            Err(e) => {
                eprintln!("[Manager-Error] Failed to start listener.exe: {}", e);
                None
            }
        }
    }

    pub fn start_processes(&mut self) {
        // ---- listener.exe ----
        if Self::is_listener_running() {
            println!("[Manager] listener.exe is already running. Skipping launch.");
        } else {
            self.listener = self.spawn_listener();
        }

        // ---- llama-server ----
        if Self::is_llama_server_ready() {
            println!("[Manager] llama-server is already running and ready. Skipping launch.");
            return;
        }

        self.llama_server = self.spawn_llama_server();

        println!("[Manager] Waiting for llama-server to become ready...");
        let client = Client::new();
        let mut attempts = 0u32;
        loop {
            // 先にTCPポートの状態を確認
            if Self::is_port_in_use(8000) {
                match client
                    .get("http://127.0.0.1:8000/v1/models")
                    .timeout(Duration::from_secs(2))
                    .send()
                {
                    Ok(resp) if resp.status().is_success() => {
                        println!("[Manager] llama-server is ready (HTTP 200).");
                        break;
                    }
                    Ok(resp) => println!("[Manager] Polling llama-server... Port Open, HTTP status={}", resp.status()),
                    Err(_) => println!("[Manager] Polling llama-server... Port Open, waiting for HTTP readiness."),
                }
            } else {
                println!("[Manager] Polling llama-server... Port 8000 is not open yet.");
            }

            attempts += 1;
            if attempts > LLAMA_READY_MAX_ATTEMPTS {
                eprintln!("[Manager-Error] llama-server did not become ready in 60 s. Continuing anyway.");
                break;
            }
            thread::sleep(Duration::from_millis(LLAMA_READY_POLL_MS));
        }
    }

    pub fn watch_and_restart(&mut self) {
        // --- llama-server ---
        let llama_dead = match &mut self.llama_server {
            Some(child) => matches!(child.try_wait(), Ok(Some(_)) | Err(_)),
            None => !Self::is_port_in_use(8000), 
        };

        if llama_dead {
            println!("[Manager-Warning] llama-server.exe seems dead or not running. Restarting...");
            self.llama_server = self.spawn_llama_server();
        }

        // --- listener.exe ---
        let listener_dead = match &mut self.listener {
            Some(child) => matches!(child.try_wait(), Ok(Some(_)) | Err(_)),
            None => !Self::is_listener_running(),
        };

        if listener_dead {
            println!("[Manager-Warning] listener.exe is not running. Restarting...");
            self.listener = self.spawn_listener();
        }
    }

    pub fn on_assistant_exit(&mut self) {
        println!("[Manager] assistant.exe is closing. llama-server and listener.exe will keep running.");
        let _ = self.llama_server.take();
        let _ = self.listener.take();
    }
}

pub fn start_background_processes(app: &AppHandle) -> Arc<Mutex<ProcessManager>> {
    let paths = match ResourcePaths::resolve(app) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[Manager-Error] {}", e);
            eprintln!(
                "[Manager-Error] Falling back to current working directory for both base and bin. \
                 listener.exe / llama-server.exe may fail to start."
            );
            let fallback = PathBuf::from(".");
            ResourcePaths {
                base: fallback.clone(),
                bin: fallback,
            }
        }
    };

    let pm = Arc::new(Mutex::new(ProcessManager::new(paths)));
    let pm_clone = pm.clone();

    thread::spawn(move || {
        {
            let mut manager = pm_clone.lock().unwrap();
            manager.start_processes();
        }

        loop {
            thread::sleep(Duration::from_secs(WATCH_INTERVAL_SECS));
            let mut manager = pm_clone.lock().unwrap();
            manager.watch_and_restart();
        }
    });

    pm
}
