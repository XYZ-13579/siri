use std::os::windows::process::CommandExt;
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use reqwest::blocking::Client;

const CREATE_NO_WINDOW: u32 = 0x08000000;

// 監視ループの間隔（秒）
// listener は assistant 側から起動するのではなく自律的に動くため、
// ここでは llama-server のみを管理する。
const WATCH_INTERVAL_SECS: u64 = 10;

// llama-server の起動を待つ最大試行回数（500 ms × 120 = 60 秒）
const LLAMA_READY_POLL_MS: u64 = 500;
const LLAMA_READY_MAX_ATTEMPTS: u32 = 120;

// -----------------------------------------------------------------------
// ProcessManager
//   - llama-server のみを管理する。
//   - listener.exe は listener 自身がシングルインスタンス制御を行うため
//     ここでは起動・監視しない。
//   - assistant.exe 終了時も llama-server はバックグラウンドで継続する。
// -----------------------------------------------------------------------
pub struct ProcessManager {
    llama_server: Option<Child>,
}

impl ProcessManager {
    pub fn new() -> Self {
        ProcessManager { llama_server: None }
    }

    // llama-server が HTTP で応答しているか確認する
    fn is_llama_server_ready() -> bool {
        let client = Client::new();
        client
            .get("http://127.0.0.1:8000/v1/models")
            .timeout(Duration::from_millis(500))
            .send()
            .map(|r| r.status().is_success())
            .unwrap_or(false)
    }

    fn spawn_llama_server() -> Option<Child> {
        println!("[Manager] Starting llama-server.exe...");
        match Command::new("llama/llama-server.exe")
            .args(&["-m", "model/LFM2.5-1.2B-Instruct-Q8_0.gguf", "--port", "8000"])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
        {
            Ok(child) => {
                println!("[Manager] llama-server started (PID={}).", child.id());
                Some(child)
            }
            Err(e) => {
                eprintln!("[Manager] Failed to start llama-server.exe: {}", e);
                None
            }
        }
    }

    /// 初回起動処理
    pub fn start_processes(&mut self) {
        // ---- llama-server ----
        if Self::is_llama_server_ready() {
            println!("[Manager] llama-server is already running. Skipping launch.");
            return;
        }

        self.llama_server = Self::spawn_llama_server();

        // llama-server が Ready になるまでポーリング
        println!("[Manager] Waiting for llama-server to become ready...");
        let client = Client::new();
        let mut attempts = 0u32;
        loop {
            match client
                .get("http://127.0.0.1:8000/v1/models")
                .timeout(Duration::from_secs(2))
                .send()
            {
                Ok(resp) if resp.status().is_success() => {
                    println!("[Manager] llama-server is ready.");
                    break;
                }
                Ok(resp) => {
                    println!("[Manager] Polling llama-server... status={}", resp.status());
                }
                Err(_) => {}
            }
            attempts += 1;
            if attempts > LLAMA_READY_MAX_ATTEMPTS {
                eprintln!("[Manager] llama-server did not become ready in 60 s. Continuing anyway.");
                break;
            }
            thread::sleep(Duration::from_millis(LLAMA_READY_POLL_MS));
        }
    }

    /// 定期監視: llama-server が落ちていたら再起動する。
    pub fn watch_and_restart(&mut self) {
        let llama_dead = match &mut self.llama_server {
            Some(child) => matches!(child.try_wait(), Ok(Some(_)) | Err(_)),
            // None = 外部起動の可能性があるため HTTP で確認
            None => !Self::is_llama_server_ready(),
        };

        if llama_dead {
            println!("[Manager] llama-server.exe is not running. Restarting...");
            self.llama_server = Self::spawn_llama_server();
        }
    }

    /// assistant.exe 終了時に呼ばれる。
    /// llama-server は意図的に終了させずバックグラウンドで継続させる。
    pub fn on_assistant_exit(&mut self) {
        println!("[Manager] assistant.exe is closing. llama-server will keep running.");
        // take() で所有権を手放すだけ（kill しない）
        let _ = self.llama_server.take();
    }
}

pub fn start_background_processes() -> Arc<Mutex<ProcessManager>> {
    let pm = Arc::new(Mutex::new(ProcessManager::new()));
    let pm_clone = pm.clone();

    thread::spawn(move || {
        {
            let mut manager = pm_clone.lock().unwrap();
            manager.start_processes();
        }

        // 監視ループ: WATCH_INTERVAL_SECS 秒ごとに llama-server の生存を確認
        loop {
            thread::sleep(Duration::from_secs(WATCH_INTERVAL_SECS));
            let mut manager = pm_clone.lock().unwrap();
            manager.watch_and_restart();
        }
    });

    pm
}
