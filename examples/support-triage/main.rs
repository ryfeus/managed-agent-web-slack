//! A local simulated CMA conversation. Only native-admitted results enter its model-facing sink.
mod native;
#[cfg(test)]
mod tests;
mod tools;

use native::{Decision, Operation, Request, Review};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    io::{self, Read, Seek, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use tools::{Call, MAX_BYTES};

type Result<T> = std::result::Result<T, &'static str>;
const POLICY: &str = include_str!("appa.toml");

struct Runner {
    _directory: tempfile::TempDir,
    database: PathBuf,
    policy: PathBuf,
    binary: PathBuf,
    seen: HashSet<String>,
    executed: Vec<Call>,
    delivered: Vec<Value>,
    processes: Vec<u32>,
    halted: bool,
}

impl Runner {
    fn new(binary: &Path, policy: &str) -> Result<Self> {
        let directory = tempfile::tempdir().map_err(|_| "temporary-state-unavailable")?;
        let database = directory.path().join("trajectory.sqlite");
        let policy_path = directory.path().join("appa.toml");
        std::fs::write(&policy_path, policy).map_err(|_| "temporary-state-unavailable")?;
        let mut runner = Self {
            _directory: directory,
            database,
            policy: policy_path,
            binary: binary.into(),
            seen: HashSet::new(),
            executed: vec![],
            delivered: vec![],
            processes: vec![],
            halted: false,
        };
        if runner.exchange(Operation::Start)? != Decision::Started {
            return Err("start-failed");
        }
        Ok(runner)
    }

    fn exchange(&mut self, operation: Operation) -> Result<Decision> {
        let request = Request {
            database: self.database.clone(),
            policy: self.policy.clone(),
            operation,
        };
        // Files avoid a pipe-buffer deadlock for larger admitted results. tempfile makes
        // these private and removes them on every return path. Stderr never carries payloads.
        let mut output = tempfile::tempfile().map_err(|_| "helper-unavailable")?;
        let mut child = Command::new(&self.binary)
            .arg("--native")
            .stdin(Stdio::piped())
            .stdout(output.try_clone().map_err(|_| "helper-unavailable")?)
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "helper-unavailable")?;
        self.processes.push(child.id());
        let bytes = serde_json::to_vec(&request).map_err(|_| "invalid-request")?;
        let sent = child
            .stdin
            .take()
            .ok_or("helper-unavailable")?
            .write_all(&bytes);
        if sent.is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("helper-unavailable");
        }
        let deadline = Instant::now() + Duration::from_secs(25);
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    if !status.success() {
                        return Err("helper-unavailable");
                    }
                    break;
                }
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(10))
                }
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("helper-timeout");
                }
            }
        }
        output.rewind().map_err(|_| "helper-unavailable")?;
        let mut bytes = vec![];
        output
            .take(512 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "helper-unavailable")?;
        if bytes.len() > 512 * 1024 {
            return Err("invalid-helper-response");
        }
        let response: Value =
            serde_json::from_slice(&bytes).map_err(|_| "invalid-helper-response")?;
        if response.get("error").is_some() {
            // Do not surface arbitrary helper diagnostics, paths, arguments or result text.
            return Err("native-policy-held");
        }
        serde_json::from_value(response).map_err(|_| "invalid-helper-response")
    }

    fn step(
        &mut self,
        call: Call,
        output: &mut impl Write,
        review: &mut impl FnMut(&Review, &Call) -> Result<bool>,
    ) -> Result<Option<String>> {
        if self.halted {
            return Err("run-held");
        }
        let result = self.step_inner(call, output, review);
        if result.is_err() {
            self.halted = true;
        }
        result
    }

    fn step_inner(
        &mut self,
        call: Call,
        output: &mut impl Write,
        review: &mut impl FnMut(&Review, &Call) -> Result<bool>,
    ) -> Result<Option<String>> {
        let prepared = call.prepare()?;
        if !self.seen.insert(call.id.clone()) {
            return Err("duplicate-invocation");
        }
        let mut decision = self.exchange(Operation::Call { call: call.clone() })?;
        if let Decision::Review { review: offer } = decision {
            writeln!(output, "{} {}: authority maintainer", call.id, call.name)
                .map_err(|_| "output-unavailable")?;
            writeln!(output, "{}\nExact call: {}", offer.description, call.input)
                .map_err(|_| "output-unavailable")?;
            output.flush().map_err(|_| "output-unavailable")?;
            let approve = review(&offer, &call)?;
            decision = self.exchange(Operation::Resolve {
                call: call.clone(),
                review: offer,
                approve,
            })?;
        }
        let (text, is_error) = match decision {
            Decision::Deny => {
                writeln!(output, "{} {}: deny (not executed)", call.id, call.name)
                    .map_err(|_| "output-unavailable")?;
                ("Denied by OpenAPPA policy.".to_string(), true)
            }
            Decision::Allow { remedy } => {
                writeln!(
                    output,
                    "{} {}: allow{}",
                    call.id,
                    call.name,
                    remedy.map(|s| format!(" ({s})")).unwrap_or_default()
                )
                .map_err(|_| "output-unavailable")?;
                // This single-process demo never retries a dispatch. It does not claim
                // the production scheduler's durable fencing or external idempotency.
                self.executed.push(call.clone());
                let raw = prepared.execute(&call.id);
                match self.exchange(Operation::Result {
                    call: call.clone(),
                    text: raw,
                })? {
                    Decision::Deliver { text } if text.len() <= MAX_BYTES => (text, false),
                    Decision::Withhold => return Err("result-withheld"),
                    _ => return Err("result-not-admitted"),
                }
            }
            _ => return Err("unexpected-call-decision"),
        };
        let result = json!({"type":"user.custom_tool_result","custom_tool_use_id":call.id,
            "content":[{"type":"text","text":text}],"is_error":is_error});
        writeln!(output, "  {result}").map_err(|_| "output-unavailable")?;
        self.delivered.push(result);
        Ok((!is_error).then_some(text))
    }

    fn scenario(
        &mut self,
        output: &mut impl Write,
        review: &mut impl FnMut(&Review, &Call) -> Result<bool>,
    ) -> Result<()> {
        let public = json!({"tracker":"public","title":"Synthetic checkout reliability",
            "body":"Investigate intermittent HTTP 503 responses in the demo checkout service."});
        self.step(
            Call::new("1", "create_issue", public.clone()),
            output,
            review,
        )?;
        let ticket = self
            .step(
                Call::new("2", "read_ticket", json!({"ticket_id":"T-4821"})),
                output,
                review,
            )?
            .ok_or("required-read-denied")?;
        let logs = self
            .step(
                Call::new("3", "fetch_logs", json!({"ticket_id":"T-4821"})),
                output,
                review,
            )?
            .ok_or("required-read-denied")?;
        self.step(
            Call::new(
                "4",
                "create_issue",
                json!({"tracker":"internal",
            "title":"Synthetic ticket T-4821 investigation","body":format!("{ticket}\n{logs}")}),
            ),
            output,
            review,
        )?;
        self.step(
            Call::new("5", "create_issue", public.clone()),
            output,
            review,
        )?;
        self.step(Call::new("6", "create_issue", public), output, review)?;
        Ok(())
    }
}

fn native_main() -> Result<()> {
    let mut bytes = vec![];
    io::stdin()
        .take(512 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "invalid-request")?;
    if bytes.len() > 512 * 1024 {
        return Err("invalid-request");
    }
    let request: Request = serde_json::from_slice(&bytes).map_err(|_| "invalid-request")?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| "helper-unavailable")?;
    let result = runtime.block_on(async {
        tokio::time::timeout(Duration::from_secs(20), native::evaluate(request))
            .await
            .map_err(|_| "policy-timeout")?
    });
    let response = match result {
        Ok(decision) => serde_json::to_value(decision).map_err(|_| "invalid-decision")?,
        Err(code) => json!({"error":code}),
    };
    println!("{response}");
    Ok(())
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--native"] {
        return native_main();
    }
    if args == ["--help"] {
        println!(
            "Usage: npm run example:support-triage -- [--review interactive|allow|deny]\nDefault: interactive terminal review. All tools and CMA delivery are simulated."
        );
        return Ok(());
    }
    let mode = match args.as_slice() {
        [] => "interactive",
        [flag, mode]
            if flag == "--review" && ["interactive", "allow", "deny"].contains(&mode.as_str()) =>
        {
            mode
        }
        _ => return Err("usage: --review interactive|allow|deny"),
    };
    let mut output = io::stdout();
    writeln!(
        output,
        "Support triage: mock tools and CMA delivery; real native OpenAPPA."
    )
    .map_err(|_| "output-unavailable")?;
    if mode != "interactive" {
        writeln!(
            output,
            "Scripted maintainer rulings: {mode} (no human consent claimed)."
        )
        .map_err(|_| "output-unavailable")?;
    }
    let mut runner = Runner::new(
        &std::env::current_exe().map_err(|_| "helper-unavailable")?,
        POLICY,
    )?;
    runner.scenario(&mut output, &mut |_, _| {
        if mode != "interactive" {
            return Ok(mode == "allow");
        }
        loop {
            print!("Maintainer ruling [allow/deny]: ");
            io::stdout().flush().map_err(|_| "output-unavailable")?;
            let mut line = String::new();
            if io::stdin()
                .read_line(&mut line)
                .map_err(|_| "review-unavailable")?
                == 0
            {
                return Err("review-required");
            }
            match line.trim().to_ascii_lowercase().as_str() {
                "allow" => return Ok(true),
                "deny" => return Ok(false),
                _ => (),
            }
        }
    })?;
    writeln!(
        output,
        "Completed: {} mock executions, {} simulated results. Temporary state removed on exit.",
        runner.executed.len(),
        runner.delivered.len()
    )
    .map_err(|_| "output-unavailable")?;
    Ok(())
}

fn main() {
    if let Err(code) = run() {
        eprintln!("Support-triage stopped: {code}. No retry or raw-output fallback.");
        std::process::exit(1);
    }
}
