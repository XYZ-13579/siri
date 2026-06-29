use std::os::windows::process::CommandExt;
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use reqwest::blocking::Client;

const CREATE_NO_WINDOW: u32 = 0x08000000;

// -----------------------------------------------------------------------
// ProcessManager
//   - llama_server と listener は assistant.exe が閉じられても終了しない。
//   - クラッシュした場合は自動的に再起動する。
// -----------------------------------------------------------------------
pub struct ProcessManager {
    llama_server: Option<Child>,
    listener: Option<Child>,
}

impl ProcessManager {
    pub fn new() -> Self {
        ProcessManager {
            llama_server: None,
            listener: None,
        }
    }

    // llama-server が既に動いているか確認する
    fn is_llama_server_ready() -> bool {
        let client = Client::new();
        client
            .get("http://127.0.0.1:8000/v1/models")
            .timeout(Duration::from_millis(500))
            .send()
            .map(|r| r.status().is_success())
            .unwrap_or(false)
    }

    // listener が既に動いているか確認する（HTTP で /show を叩けるかではなく
    // assistant 側の :5678 が listen しているかとは独立して、プロセス名で判定）
    fn is_listener_running() -> bool {
        // tasklist で listener.exe が動いているか確認
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

    pub fn start_processes(&mut self) {
        // ---- llama-server ----
        if Self::is_llama_server_ready() {
            println!("[Manager] llama-server is already running. Skipping launch.");
        } else {
            println!("[Manager] Starting llama-server.exe...");
            match Command::new("llama/llama-server.exe")
                .args(&["-m", "model/LFM2.5-1.2B-Instruct-Q8_0.gguf", "--port", "8000"])
                .creation_flags(CREATE_NO_WINDOW)
                .spawn()
            {
                Ok(child) => {
                    println!("[Manager] llama-server started (PID={}).", child.id());
                    self.llama_server = Some(child);
                }
                Err(e) => {
                    eprintln!("[Manager] Failed to start llama-server.exe: {}", e);
                }
            }

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
                if attempts > 120 {
                    eprintln!("[Manager] llama-server did not become ready in 60 s. Continuing anyway.");
                    break;
                }
                thread::sleep(Duration::from_millis(500));
            }
        }

        // ---- listener ----
        if Self::is_listener_running() {
            println!("[Manager] listener.exe is already running. Skipping launch.");
        } else {
            self.launch_listener();
        }
    }

    fn launch_listener(&mut self) {
        println!("[Manager] Starting listener.exe...");
        match Command::new("listener.exe")
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
        {
            Ok(child) => {
                println!("[Manager] listener started (PID={}).", child.id());
                self.listener = Some(child);
            }
            Err(e) => {
                eprintln!("[Manager] Failed to start listener.exe: {}", e);
            }
        }
    }

    /// listener が生きているか確認し、落ちていたら再起動する。
    /// llama-server も同様に監視して再起動する。
    pub fn watch_and_restart(&mut self) {
        // --- listener の監視 ---
        let listener_dead = match &mut self.listener {
            Some(child) => matches!(child.try_wait(), Ok(Some(_)) | Err(_)),
            None => true,
        };
        if listener_dead {
            if self.listener.is_some() {
                println!("[Manager] listener.exe exited unexpectedly. Restarting...");
            }
            self.listener = None;
            self.launch_listener();
        }

        // --- llama-server の監視 ---
        let llama_dead = match &mut self.llama_server {
            Some(child) => matches!(child.try_wait(), Ok(Some(_)) | Err(_)),
            None => false, // None の場合は外部起動かもしれないので確認しない
        };
        if llama_dead {
            println!("[Manager] llama-server.exe exited unexpectedly. Restarting...");
            self.llama_server = None;
            match Command::new("llama/llama-server.exe")
                .args(&["-m", "model/LFM2.5-1.2B-Instruct-Q8_0.gguf", "--port", "8000"])
                .creation_flags(CREATE_NO_WINDOW)
                .spawn()
            {
                Ok(child) => {
                    println!("[Manager] llama-server restarted (PID={}).", child.id());
                    self.llama_server = Some(child);
                }
                Err(e) => {
                    eprintln!("[Manager] Failed to restart llama-server.exe: {}", e);
                }
            }
        }
    }

    /// assistant.exe 終了時に呼ばれる。
    /// listener と llama-server は意図的に終了させない（バックグラウンド継続）。
    pub fn on_assistant_exit(&mut self) {
        println!("[Manager] assistant.exe is closing. listener and llama-server will keep running in background.");
        // Child の所有権を放棄して、プロセスを kill せずに継続させる
        // drop するだけで kill は呼ばれない（Child::drop は wait/kill しない）
        let _ = self.llama_server.take();
        let _ = self.listener.take();
    }
}

pub fn start_background_processes() -> Arc<Mutex<ProcessManager>> {
    let pm = Arc::new(Mutex::new(ProcessManager::new()));
    let pm_clone = pm.clone();

    // 初回起動スレッド
    thread::spawn(move || {
        {
            let mut manager = pm_clone.lock().unwrap();
            manager.start_processes();
        }

        // 監視ループ: 5 秒ごとにプロセスの生存を確認し、落ちていたら再起動
        loop {
            thread::sleep(Duration::from_secs(5));
            let mut manager = pm_clone.lock().unwrap();
            manager.watch_and_restart();
        }
    });

    pm
}
