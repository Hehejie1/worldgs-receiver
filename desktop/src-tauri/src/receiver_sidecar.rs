use anyhow::{anyhow, Context, Result};
use portpicker::pick_unused_port;
use reqwest::Client;
use std::path::PathBuf;
use std::process::Command as StdCommand;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager, WebviewWindow};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const SIDECAR_NAME: &str = "receiver_sidecar";
const HEALTHCHECK_PATH: &str = "/api/healthz";
const HEALTHCHECK_TIMEOUT: Duration = Duration::from_secs(30);
const HEALTHCHECK_INTERVAL: Duration = Duration::from_millis(500);

pub struct ReceiverHandle {
    port: u16,
    root_pid: u32,
    child: CommandChild,
}

#[derive(Default)]
pub struct ReceiverState(pub Mutex<Option<ReceiverHandle>>);

pub async fn start_receiver_sidecar(app: &AppHandle) -> Result<String> {
    if let Some(port) = active_port(app) {
        if healthcheck_ready(port, Duration::from_secs(1)).await? {
            return Ok(receiver_url(port));
        }
        cleanup_receiver_sidecar(app);
    }

    cleanup_stale_receiver_sidecars(app);
    let output_dir = receiver_output_dir(app)?;
    let mut last_error: Option<anyhow::Error> = None;

    for attempt in 1..=2 {
        let port = pick_unused_port().ok_or_else(|| anyhow!("无法分配可用本地端口"))?;
        let _ = kill_listener_on_port(port);

        match spawn_receiver_sidecar(app, port, &output_dir).await {
            Ok(()) => return Ok(receiver_url(port)),
            Err(error) => {
                last_error =
                    Some(error.context(format!("Receiver sidecar 第 {attempt} 次启动失败")));
                cleanup_stale_receiver_sidecars(app);
            }
        }
    }

    Err(last_error.unwrap_or_else(|| anyhow!("Receiver sidecar 启动失败")))
}

pub fn cleanup_receiver_sidecar(app: &AppHandle) {
    let existing = {
        let state = app.state::<ReceiverState>();
        let mut guard = state.0.lock().expect("receiver sidecar state poisoned");
        guard.take()
    };
    let Some(handle) = existing else {
        return;
    };

    let _ = kill_process_tree(handle.root_pid);
    let _ = handle.child.kill();
    let _ = kill_listener_on_port(handle.port);
}

pub fn navigate_main_window(window: &WebviewWindow, url: &str) -> tauri::Result<()> {
    let script = format!(
        "window.location.replace({});",
        serde_json::to_string(url).expect("valid navigation url")
    );
    window.eval(&script)
}

pub fn render_startup_error(window: &WebviewWindow, message: &str) -> tauri::Result<()> {
    let text = format!("启动 Receiver 失败。\\n\\n{message}");
    let script = format!(
        "const el = document.getElementById('status'); if (el) {{ el.textContent = {}; }}",
        serde_json::to_string(&text).expect("valid startup message")
    );
    window.eval(&script)
}

fn active_port(app: &AppHandle) -> Option<u16> {
    let state = app.state::<ReceiverState>();
    let guard = state.0.lock().ok()?;
    guard.as_ref().map(|item| item.port)
}

fn receiver_output_dir(app: &AppHandle) -> Result<PathBuf> {
    let base = app
        .path()
        .app_local_data_dir()
        .or_else(|_| app.path().app_data_dir())
        .context("无法解析桌面端数据目录")?;
    let output_dir = base.join("WorldGS_Imports");
    std::fs::create_dir_all(&output_dir).context("创建 Receiver 输出目录失败")?;
    Ok(output_dir)
}

fn playwright_browsers_path(app: &AppHandle) -> Option<PathBuf> {
    let path = app.path().resource_dir().ok()?.join("playwright-browsers");
    path.exists().then_some(path)
}

async fn spawn_receiver_sidecar(app: &AppHandle, port: u16, output_dir: &PathBuf) -> Result<()> {
    let args = sidecar_args(port, output_dir);
    let mut command = app
        .shell()
        .sidecar(SIDECAR_NAME)
        .context("未找到 Receiver sidecar，可先执行 sidecar 构建脚本")?
        .args(args.clone());
    if let Some(browsers_path) = playwright_browsers_path(app) {
        command = command.env("PLAYWRIGHT_BROWSERS_PATH", browsers_path);
    }
    let (mut rx, child) = command.spawn().context("启动 Receiver sidecar 失败")?;
    let root_pid = child.pid();

    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    println!("[receiver-sidecar] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Stderr(line) => {
                    eprintln!("[receiver-sidecar] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Error(line) => {
                    eprintln!("[receiver-sidecar] {line}");
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!(
                        "[receiver-sidecar] exited: code={:?} signal={:?}",
                        payload.code, payload.signal
                    );
                    break;
                }
                _ => {}
            }
        }
    });

    if let Err(error) = wait_for_healthcheck(port).await {
        let _ = kill_process_tree(root_pid);
        let _ = child.kill();
        let _ = kill_listener_on_port(port);
        return Err(error.context(format!(
            "Receiver sidecar 启动后未通过健康检查，port={port}"
        )));
    }

    let state = app.state::<ReceiverState>();
    let mut guard = state.0.lock().expect("receiver sidecar state poisoned");
    *guard = Some(ReceiverHandle {
        port,
        root_pid,
        child,
    });
    Ok(())
}

async fn wait_for_healthcheck(port: u16) -> Result<()> {
    let client = healthcheck_client(Duration::from_secs(2))?;
    let started_at = std::time::Instant::now();

    loop {
        let url = healthcheck_url(port);
        if started_at.elapsed() > HEALTHCHECK_TIMEOUT {
            return Err(anyhow!("Receiver sidecar 启动超时：{url}"));
        }

        if healthcheck_ready_with_client(&client, &url).await {
            return Ok(());
        }

        tokio::time::sleep(HEALTHCHECK_INTERVAL).await;
    }
}

fn healthcheck_client(timeout: Duration) -> Result<Client> {
    Client::builder()
        .timeout(timeout)
        .build()
        .context("初始化健康检查 HTTP 客户端失败")
}

async fn healthcheck_ready(port: u16, timeout: Duration) -> Result<bool> {
    let client = healthcheck_client(timeout)?;
    Ok(healthcheck_ready_with_client(&client, &healthcheck_url(port)).await)
}

async fn healthcheck_ready_with_client(client: &Client, url: &str) -> bool {
    if let Ok(response) = client.get(url).send().await {
        return response.status().is_success();
    }
    false
}

fn receiver_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/")
}

fn healthcheck_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}{HEALTHCHECK_PATH}")
}

fn sidecar_args(port: u16, output_dir: &PathBuf) -> Vec<String> {
    vec![
        "--host".into(),
        "0.0.0.0".into(),
        "--port".into(),
        port.to_string(),
        "--output".into(),
        output_dir.display().to_string(),
    ]
}

fn cleanup_stale_receiver_sidecars(_app: &AppHandle) {
    #[cfg(not(target_os = "windows"))]
    {
        let _ = cleanup_stale_receiver_sidecars_unix();
    }
}

#[cfg(not(target_os = "windows"))]
fn cleanup_stale_receiver_sidecars_unix() -> Result<()> {
    let sidecar_path = std::env::current_exe()
        .context("无法解析桌面端当前可执行文件路径")?
        .with_file_name(SIDECAR_NAME);
    let sidecar_path = sidecar_path.to_string_lossy().into_owned();
    let output = StdCommand::new("ps")
        .args(["-axww", "-o", "pid=,ppid=,command="])
        .output()
        .context("读取 sidecar 进程列表失败")?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    for process in parse_ps_processes(&stdout) {
        if process.ppid != 1 {
            continue;
        }
        if !process.command.starts_with(&sidecar_path) {
            continue;
        }
        let _ = kill_process_tree(process.pid);
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn parse_ps_processes(output: &str) -> Vec<PsProcess> {
    output
        .lines()
        .filter_map(PsProcess::parse)
        .collect::<Vec<_>>()
}

#[cfg(not(target_os = "windows"))]
#[derive(Debug, Clone, PartialEq, Eq)]
struct PsProcess {
    pid: u32,
    ppid: u32,
    command: String,
}

#[cfg(not(target_os = "windows"))]
impl PsProcess {
    fn parse(line: &str) -> Option<Self> {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            return None;
        }
        let pid_end = trimmed.find(char::is_whitespace)?;
        let pid = trimmed[..pid_end].trim().parse().ok()?;
        let rest = trimmed[pid_end..].trim_start();
        let ppid_end = rest.find(char::is_whitespace)?;
        let ppid = rest[..ppid_end].trim().parse().ok()?;
        let command = rest[ppid_end..].trim().to_string();
        if command.is_empty() {
            return None;
        }
        Some(Self { pid, ppid, command })
    }
}

#[cfg(target_os = "windows")]
fn kill_listener_on_port(port: u16) -> Result<()> {
    let script = format!(
        "$pids = netstat -ano | Select-String ':{} ' | Select-String 'LISTENING' | ForEach-Object {{ ($_ -split '\\s+')[-1] }} | Select-Object -Unique; foreach ($pid in $pids) {{ taskkill /PID $pid /T /F | Out-Null }}",
        port
    );
    let _ = StdCommand::new("powershell")
        .args(["-NoProfile", "-Command", &script])
        .status()
        .context("清理占用端口的 Windows sidecar 失败")?;
    Ok(())
}

#[cfg(target_os = "windows")]
fn kill_process_tree(pid: u32) -> Result<()> {
    let _ = StdCommand::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .status()
        .context("结束 Windows sidecar 进程树失败")?;
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn kill_listener_on_port(port: u16) -> Result<()> {
    let output = StdCommand::new("lsof")
        .args(["-tiTCP", &port.to_string(), "-sTCP:LISTEN"])
        .output()
        .context("查询占用端口的 sidecar 进程失败")?;
    let pids = String::from_utf8_lossy(&output.stdout);
    for pid in pids.lines().filter(|line| !line.trim().is_empty()) {
        let _ = StdCommand::new("kill").args(["-TERM", pid]).status();
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn kill_process_tree(pid: u32) -> Result<()> {
    for child_pid in child_pids(pid)? {
        let _ = kill_process_tree(child_pid);
    }
    let _ = StdCommand::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status();
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn child_pids(pid: u32) -> Result<Vec<u32>> {
    let output = StdCommand::new("pgrep")
        .args(["-P", &pid.to_string()])
        .output()
        .context("查询 sidecar 子进程失败")?;
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| line.trim().parse::<u32>().ok())
        .collect::<Vec<_>>())
}

#[cfg(test)]
mod tests {
    use super::{healthcheck_url, sidecar_args};
    use std::path::PathBuf;
    use std::time::Duration;

    #[cfg(not(target_os = "windows"))]
    use super::{parse_ps_processes, PsProcess};

    #[tokio::test]
    async fn healthcheck_ready_returns_false_for_closed_port() {
        let ready = super::healthcheck_ready(9, Duration::from_millis(50))
            .await
            .expect("healthcheck client should initialize");

        assert!(!ready);
    }

    #[test]
    fn healthcheck_url_targets_loopback_endpoint() {
        assert_eq!(healthcheck_url(8878), "http://127.0.0.1:8878/api/healthz");
    }

    #[test]
    fn sidecar_args_lock_host_port_and_output() {
        let args = sidecar_args(8878, &PathBuf::from("/tmp/worldgs"));

        assert_eq!(
            args,
            vec![
                "--host",
                "0.0.0.0",
                "--port",
                "8878",
                "--output",
                "/tmp/worldgs"
            ]
        );
    }

    #[cfg(not(target_os = "windows"))]
    #[test]
    fn parse_ps_processes_reads_pid_ppid_and_command() {
        let processes = parse_ps_processes(
            " 15545     1 /Applications/WorldGS.app/Contents/MacOS/receiver_sidecar --host 0.0.0.0 --port 18158\n",
        );

        assert_eq!(
            processes,
            vec![PsProcess {
                pid: 15545,
                ppid: 1,
                command: "/Applications/WorldGS.app/Contents/MacOS/receiver_sidecar --host 0.0.0.0 --port 18158"
                    .into(),
            }]
        );
    }

    #[cfg(not(target_os = "windows"))]
    #[test]
    fn parse_ps_processes_skips_invalid_lines() {
        let processes = parse_ps_processes("bad line\n 42 1 \n");

        assert!(processes.is_empty());
    }
}
