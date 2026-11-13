use super::*;

fn binary() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("target/debug/support-triage")
}

fn runner(policy: &str) -> Runner {
    Runner::new(&binary(), policy).expect("start native trajectory")
}
fn ticket(id: &str) -> Call {
    Call::new(id, "read_ticket", json!({"ticket_id":"T-4821"}))
}
fn logs(id: &str) -> Call {
    Call::new(id, "fetch_logs", json!({"ticket_id":"T-4821"}))
}
fn no_review(_: &Review, _: &Call) -> Result<bool> {
    panic!("unexpected human review")
}
fn masked() -> String {
    tools::RAW_LOGS.replace("FAKE-support-triage-not-a-credential", "[redacted-secret]")
}

#[test]
fn six_steps_reopen_native_state_and_require_two_separate_approvals() {
    let mut run = runner(POLICY);
    let directory = run._directory.path().to_owned();
    let mut output = vec![];
    let mut reviews = vec![];
    run.scenario(&mut output, &mut |review, call| {
        reviews.push((review.clone(), call.clone()));
        Ok(true)
    })
    .unwrap();
    assert_eq!(run.executed.len(), 6);
    assert_eq!(run.delivered.len(), 6);
    assert_eq!(reviews.len(), 2);
    assert_ne!(reviews[0].0.fingerprint, reviews[1].0.fingerprint);
    assert_eq!(run.executed[0].input, reviews[0].1.input);
    assert_eq!(reviews[0].1.input, reviews[1].1.input);
    assert_eq!(run.delivered[2]["content"][0]["text"], masked());
    assert_eq!(
        run.executed[3].input["body"],
        format!("{}\n{}", tools::TICKET, masked())
    );
    for (index, result) in run.delivered.iter().enumerate() {
        assert_eq!(result["type"], "user.custom_tool_result");
        assert_eq!(result["custom_tool_use_id"], (index + 1).to_string());
        assert_eq!(result["is_error"], false);
    }
    let rendered = String::from_utf8(output).unwrap();
    for data in [
        rendered,
        serde_json::to_string(&run.delivered).unwrap(),
        serde_json::to_string(&run.executed).unwrap(),
    ] {
        assert!(!data.contains("FAKE-support-triage-not-a-credential"));
        assert!(data.contains("[redacted-secret]"));
    }
    assert_eq!(run.processes.len(), 15); // start + 6 calls + 6 results + 2 rulings
    assert_eq!(run.processes.iter().collect::<HashSet<_>>().len(), 15);
    drop(run);
    assert!(!directory.exists());
}

#[test]
fn deny_skips_execution_and_identical_native_denial_persists() {
    let mut run = runner(POLICY);
    let mut reviews = 0;
    run.scenario(&mut vec![], &mut |_, _| {
        reviews += 1;
        Ok(false)
    })
    .unwrap();
    assert_eq!(reviews, 1);
    assert_eq!(run.executed.len(), 4);
    assert_eq!(run.delivered.len(), 6);
    for result in &run.delivered[4..] {
        assert_eq!(result["is_error"], true);
        assert_eq!(result["content"][0]["text"], "Denied by OpenAPPA policy.");
    }
}

#[test]
fn removing_maintainer_denies_public_issues_without_review() {
    let mut policy: toml::Value = toml::from_str(POLICY).unwrap();
    policy["policy"].as_table_mut().unwrap().remove("authority");
    policy["externals"]
        .as_table_mut()
        .unwrap()
        .remove("authorities");
    let mut run = runner(&toml::to_string(&policy).unwrap());
    run.scenario(&mut vec![], &mut no_review).unwrap();
    assert_eq!(run.executed.len(), 4);
    assert_eq!(run.delivered[5]["is_error"], true);
}

#[test]
fn repeated_logs_are_masked_and_replayed_admission_never_executes_again() {
    let mut run = runner(POLICY);
    run.step(ticket("ticket"), &mut vec![], &mut no_review)
        .unwrap();
    for id in ["logs-1", "logs-2"] {
        assert_eq!(
            run.step(logs(id), &mut vec![], &mut no_review).unwrap(),
            Some(masked())
        );
    }
    let admitted = run
        .exchange(Operation::Result {
            call: logs("logs-2"),
            text: tools::RAW_LOGS.into(),
        })
        .unwrap();
    assert_eq!(admitted, Decision::Deliver { text: masked() });
    assert_eq!(run.executed.len(), 3);
    assert_eq!(run.delivered.len(), 3);
    assert!(
        run.exchange(Operation::Result {
            call: logs("logs-2"),
            text: "substituted output".into()
        })
        .is_err()
    );
    assert_eq!(
        run.step(logs("logs-2"), &mut vec![], &mut no_review),
        Err("duplicate-invocation")
    );
    assert_eq!(run.executed.len(), 3);
}

#[test]
fn invalid_inputs_never_reach_policy_or_handlers() {
    for (name, input) in [
        ("read_ticket", json!({})),
        ("read_ticket", json!({"ticket_id":42})),
        ("read_ticket", json!({"ticket_id":"T-unknown"})),
        ("fetch_logs", json!({"ticket_id":"T-4821","extra":true})),
        (
            "create_issue",
            json!({"tracker":"external","title":"x","body":"y"}),
        ),
        (
            "create_issue",
            json!({"tracker":"public","title":false,"body":"y"}),
        ),
        (
            "create_issue",
            json!({"tracker":"public","title":"x","body":"y","account":"other"}),
        ),
        (
            "create_issue",
            json!({"tracker":"public","title":"x","body":"é".repeat(MAX_BYTES)}),
        ),
        ("unregistered", json!({})),
    ] {
        let mut run = runner(POLICY);
        assert!(
            run.step(
                Call::new("invalid", name, input),
                &mut vec![],
                &mut no_review
            )
            .is_err()
        );
        assert!(run.executed.is_empty());
        assert!(run.delivered.is_empty());
        assert_eq!(run.processes.len(), 1);
    }
    assert!(
        serde_json::from_value::<Call>(
            json!({"id":"1","type":"agent.tool_use","name":"read_ticket","input":{}})
        )
        .is_err()
    );
}

#[test]
fn missing_state_is_not_recreated_and_policy_changes_do_not_reset_history() {
    let mut run = runner(POLICY);
    run.step(ticket("ticket"), &mut vec![], &mut no_review)
        .unwrap();
    std::fs::remove_file(&run.database).unwrap();
    assert!(run.step(logs("logs"), &mut vec![], &mut no_review).is_err());
    assert!(!run.database.exists());
    assert_eq!(run.executed.len(), 1);

    let mut changed = runner(POLICY);
    std::fs::write(&changed.policy, format!("{POLICY}\n# changed")).unwrap();
    assert!(
        changed
            .step(ticket("ticket"), &mut vec![], &mut no_review)
            .is_err()
    );
    assert!(changed.executed.is_empty());

    let mut corrupt = runner(POLICY);
    std::fs::write(&corrupt.database, b"not a SQLite trajectory").unwrap();
    assert!(
        corrupt
            .step(ticket("ticket"), &mut vec![], &mut no_review)
            .is_err()
    );
    assert!(corrupt.executed.is_empty());
    assert_eq!(
        std::fs::read(&corrupt.database).unwrap(),
        b"not a SQLite trajectory"
    );
}

#[test]
fn withholding_never_delivers_raw_logs_and_halts_following_steps() {
    let policy = POLICY
        .replace(
            "starting_label = { audience = [\"public\"] }",
            "starting_label = { audience = [\"internal\"] }",
        )
        .replace("max_body_bytes = 65536", "max_body_bytes = 16");
    let mut run = runner(&policy);
    let mut output = vec![];
    assert_eq!(
        run.step(logs("logs"), &mut output, &mut no_review),
        Err("result-withheld")
    );
    assert_eq!(run.executed.len(), 1);
    assert!(run.delivered.is_empty());
    assert!(
        !String::from_utf8(output)
            .unwrap()
            .contains("FAKE-support-triage-not-a-credential")
    );
    assert_eq!(
        run.step(ticket("later"), &mut vec![], &mut no_review),
        Err("run-held")
    );
    assert_eq!(run.executed.len(), 1);
}

#[test]
fn unavailable_or_unsupported_maskers_never_fall_back_to_raw_acceptance() {
    let mut absent: toml::Value = toml::from_str(POLICY).unwrap();
    absent["policy"].as_table_mut().unwrap().remove("sanitizer");
    absent["externals"]
        .as_table_mut()
        .unwrap()
        .remove("sanitizers");
    let mut run = runner(&toml::to_string(&absent).unwrap());
    run.step(ticket("ticket"), &mut vec![], &mut no_review)
        .unwrap();
    assert!(run.step(logs("logs"), &mut vec![], &mut no_review).is_err());
    assert_eq!(run.executed.len(), 1);

    for policy in [
        POLICY.replace("builtin = \"redact-secrets\"", "command = [\"false\"]"),
        POLICY.replace("builtin = \"redact-secrets\"", "builtin = \"redact-email\""),
    ] {
        assert!(Runner::new(&binary(), &policy).is_err());
    }
    let mut input = runner(&POLICY.replace("on = [\"tool_output\"]", "on = [\"tool_input\"]"));
    input
        .step(ticket("ticket"), &mut vec![], &mut no_review)
        .unwrap();
    assert!(
        input
            .step(logs("logs"), &mut vec![], &mut no_review)
            .is_err()
    );
    assert_eq!(input.executed.len(), 1);
}

#[test]
fn stale_review_and_unauthorized_result_are_rejected() {
    let mut run = runner(POLICY);
    run.step(ticket("ticket"), &mut vec![], &mut no_review)
        .unwrap();
    let call = Call::new(
        "issue",
        "create_issue",
        json!({"tracker":"public","title":"x","body":"y"}),
    );
    let Decision::Review { mut review } = run
        .exchange(Operation::Call { call: call.clone() })
        .unwrap()
    else {
        panic!("expected review")
    };
    review.fingerprint = "0".repeat(64);
    assert!(
        run.exchange(Operation::Resolve {
            call: call.clone(),
            review,
            approve: true
        })
        .is_err()
    );
    assert!(
        run.exchange(Operation::Result {
            call,
            text: "unexecuted".into()
        })
        .is_err()
    );
    assert_eq!(run.executed.len(), 1);
}

#[test]
fn ambiguous_masker_configuration_and_unsanitized_allow_fail_closed() {
    let mut ambiguous: toml::Value = toml::from_str(POLICY).unwrap();
    let mut second = ambiguous["policy"]["sanitizer"][0].clone();
    second["name"] = "second-mask".into();
    ambiguous["policy"]["sanitizer"]
        .as_array_mut()
        .unwrap()
        .push(second);
    let binding = ambiguous["externals"]["sanitizers"]["mask-secrets"].clone();
    ambiguous["externals"]["sanitizers"]
        .as_table_mut()
        .unwrap()
        .insert("second-mask".into(), binding);
    assert!(Runner::new(&binary(), &toml::to_string(&ambiguous).unwrap()).is_err());

    // With an already-self audience no narrowing offer is necessary. The example
    // must still refuse a plain AllowCall for logs instead of delivering raw text.
    let mut run = runner(&POLICY.replace(
        "starting_label = { audience = [\"public\"] }",
        "starting_label = { audience = [\"self\"] }",
    ));
    assert!(run.step(logs("logs"), &mut vec![], &mut no_review).is_err());
    assert!(run.executed.is_empty());
    assert!(run.delivered.is_empty());
}

#[test]
fn cli_modes_show_admitted_output_and_eof_never_approves() {
    for mode in ["allow", "deny"] {
        let result = Command::new(binary())
            .args(["--review", mode])
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let stdout = String::from_utf8(result.stdout).unwrap();
        assert!(stdout.contains(&format!("Scripted maintainer rulings: {mode}")));
        assert!(!stdout.contains("FAKE-support-triage-not-a-credential"));
        assert!(stdout.contains("[redacted-secret]"));
        assert_eq!(
            stdout.matches("authority maintainer\n").count(),
            if mode == "allow" { 2 } else { 1 }
        );
    }
    let result = Command::new(binary())
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!result.status.success());
    assert!(
        String::from_utf8(result.stderr)
            .unwrap()
            .contains("review-required")
    );
    assert!(
        !String::from_utf8(result.stdout)
            .unwrap()
            .contains("maintainer approved this call")
    );
}
