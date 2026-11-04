//! Stateless transport only. All authorization belongs to OpenAPPA.
use appa_eventlog::{Backend, LogStore};
use appa_runtime::{
    api::{RemedyOutcome, Runtime},
    config::Config,
    hooks,
};
use appa_runtime_api::{
    Actor, CONTROL_TOOL, HookDecision, HookEvent, ProposedCall, Ruling, TrajectoryId,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    io::{self, Read, Write},
    sync::Arc,
};

#[derive(Debug, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum RuntimeError {
    InvalidRequest,
    InvalidPolicy,
    UnsupportedPolicy,
    RuntimeUnavailable,
    UnsupportedRemedy,
    StaleReview,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    version: u32,
    policy: String,
    request: Call,
    resolve: Option<Resolution>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Resolution {
    fingerprint: String,
    authority: String,
    ruling: Ruling,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Call {
    root_id: String,
    invocation_id: String,
    agent_id: String,
    tool_kind: String,
    tool_name: String,
    arguments: Value,
    runtime: String,
}
async fn evaluate(input: Request) -> Result<Value, RuntimeError> {
    let call = input.request;
    if input.version != 3
        || call.root_id.is_empty()
        || call.invocation_id.is_empty()
        || call.agent_id.is_empty()
        || call.runtime != "cma"
        || !call.arguments.is_object()
    {
        return Err(RuntimeError::InvalidRequest);
    }
    let tool = match call.tool_kind.as_str() {
        "builtin" => format!("host/cma/{}", call.tool_name),
        "mcp" => call.tool_name.clone(),
        _ => return Err(RuntimeError::InvalidRequest),
    };
    appa_runtime_api::CanonicalTool::parse(&tool).map_err(|_| RuntimeError::InvalidRequest)?;
    // This hook cannot mediate results. Reject policies requiring unsupported controls.
    let mut document: toml::Value =
        toml::from_str(&input.policy).map_err(|_| RuntimeError::InvalidPolicy)?;
    let policy = document.get("policy").ok_or(RuntimeError::InvalidPolicy)?;
    if let Some(deployment) = policy.get("deployment") {
        if deployment
            .get("context_control")
            .and_then(toml::Value::as_bool)
            .unwrap_or(false)
            || deployment
                .get("confined_results")
                .and_then(toml::Value::as_array)
                .is_some_and(|v| !v.is_empty())
            || deployment
                .get("provider_run_tools")
                .and_then(toml::Value::as_array)
                .is_some_and(|v| !v.is_empty())
        {
            return Err(RuntimeError::UnsupportedPolicy);
        }
    }
    if let Some(tools) = policy.get("tool").and_then(toml::Value::as_array) {
        for tool in tools {
            if tool
                .get("delta")
                .and_then(toml::Value::as_table)
                .is_some_and(|v| !v.is_empty())
                || tool
                    .get("effects")
                    .and_then(toml::Value::as_array)
                    .is_some_and(|v| !v.is_empty())
                || tool
                    .get("requires")
                    .and_then(|v| v.get("effects"))
                    .and_then(toml::Value::as_table)
                    .is_some_and(|v| {
                        v.values()
                            .any(|items| items.as_array().is_some_and(|a| !a.is_empty()))
                    })
            {
                return Err(RuntimeError::UnsupportedPolicy);
            }
        }
    }
    if let Some(annotators) = policy.get("annotator").and_then(toml::Value::as_array) {
        for annotator in annotators {
            // Omission grants the upstream annotator the whole vocabulary. Require
            // explicitly empty mandates so it cannot introduce deltas or history.
            for field in ["ranks", "audiences", "effects"] {
                if !annotator
                    .get(field)
                    .and_then(toml::Value::as_array)
                    .is_some_and(|v| v.is_empty())
                {
                    return Err(RuntimeError::UnsupportedPolicy);
                }
            }
        }
    }
    if policy
        .get("sanitizer")
        .and_then(toml::Value::as_array)
        .is_some_and(|v| !v.is_empty())
    {
        return Err(RuntimeError::UnsupportedPolicy);
    }
    if let Some(authorities) = policy.get("authority").and_then(toml::Value::as_array) {
        for authority in authorities {
            if authority
                .get("permits")
                .and_then(|v| v.get("effects_containing"))
                .and_then(toml::Value::as_array)
                .is_some_and(|v| !v.is_empty())
            {
                return Err(RuntimeError::UnsupportedPolicy);
            }
        }
    }
    // The supported remedy is deliberately one declared host HITL authority.
    // A native review is still required below; declaration alone grants nothing.
    let authority = policy
        .get("authority")
        .and_then(toml::Value::as_array)
        .filter(|items| items.len() == 1)
        .and_then(|items| items[0].get("name"))
        .and_then(toml::Value::as_str)
        .filter(|name| {
            document
                .get("externals")
                .and_then(|v| v.get("authorities"))
                .and_then(|v| v.get(*name))
                .and_then(|v| v.get("builtin"))
                .and_then(toml::Value::as_str)
                == Some("hitl")
        })
        .map(str::to_owned);
    let fingerprint = format!(
        "{:x}",
        Sha256::digest(
            serde_json::to_vec(&json!({"call":call,"policy":input.policy,"authority":authority}))
                .map_err(|_| RuntimeError::InvalidRequest)?
        )
    );
    if let Some(resolve) = &input.resolve {
        if resolve.fingerprint != fingerprint || Some(&resolve.authority) != authority.as_ref() {
            return Err(RuntimeError::StaleReview);
        }
    }
    // Resolve the declarative wildcard annotator to this deployed executable.
    if let Some(command) = document
        .get_mut("externals")
        .and_then(|v| v.get_mut("annotators"))
        .and_then(|v| v.get_mut("deny-undeclared"))
        .and_then(|v| v.get_mut("command"))
        .and_then(toml::Value::as_array_mut)
    {
        if command.first().and_then(toml::Value::as_str) == Some("@openappa-cma") {
            let executable =
                std::env::current_exe().map_err(|_| RuntimeError::RuntimeUnavailable)?;
            #[cfg(test)]
            let executable = executable
                .parent()
                .and_then(|p| p.parent())
                .ok_or(RuntimeError::RuntimeUnavailable)?
                .join("openappa-cma");
            command[0] = toml::Value::String(executable.to_string_lossy().into());
        }
    }
    let mut config_file =
        tempfile::NamedTempFile::new().map_err(|_| RuntimeError::RuntimeUnavailable)?;
    config_file
        .write_all(
            toml::to_string(&document)
                .map_err(|_| RuntimeError::InvalidPolicy)?
                .as_bytes(),
        )
        .map_err(|_| RuntimeError::RuntimeUnavailable)?;
    let config =
        Config::load_from(config_file.path(), &[]).map_err(|_| RuntimeError::InvalidPolicy)?;
    drop(config_file);
    let store =
        Arc::new(LogStore::open(Backend::Memory).map_err(|_| RuntimeError::RuntimeUnavailable)?);
    let runtime =
        Runtime::open_with_store(config, store, None).map_err(|_| RuntimeError::InvalidPolicy)?;
    let root = TrajectoryId(call.root_id.clone());
    let actor = Actor {
        root: root.clone(),
        child: None,
    };
    let start = hooks::handle_embedded(
        &runtime,
        HookEvent::SessionStart {
            root: root.clone(),
            principal: None,
            address: None,
            title: None,
        },
    )
    .await;
    if start.decision != HookDecision::Ack {
        return Err(RuntimeError::RuntimeUnavailable);
    }
    let result = hooks::handle_embedded(
        &runtime,
        HookEvent::ToolCall {
            actor: actor.clone(),
            call: ProposedCall {
                tool: tool.clone(),
                cwd: None,
                arguments: serde_json::value::to_raw_value(&call.arguments)
                    .map_err(|_| RuntimeError::InvalidRequest)?,
            },
            call_id: Some(call.invocation_id),
            spawn: None,
            prompt: None,
            ruling: None,
        },
    )
    .await;
    let decision = match result.decision {
        HookDecision::AllowCall { spawn: None } if input.resolve.is_none() => {
            json!({"decision":"allow","reason":null})
        }
        HookDecision::DenyCall { offers, review, .. } if offers.is_empty() && review.is_empty() => {
            if input.resolve.is_some() {
                return Err(RuntimeError::StaleReview);
            }
            json!({"decision":"deny","reason":"Denied by OpenAPPA policy."})
        }
        HookDecision::DenyCall { offers, review, .. }
            if offers.len() == 1
                && review.len() == 1
                && authority.is_some()
                && review[0].offer == offers[0].id
                && !review[0].text.is_empty()
                && offers[0].returns.is_none()
                && offers[0].input_sanitizer.is_none() =>
        {
            if let Some(resolve) = input.resolve {
                // Offer IDs belong to this fresh store, never to the UI or a previous process.
                let args = json!({"offer_id":offers[0].id});
                let gate = hooks::handle_embedded(
                    &runtime,
                    HookEvent::ToolCall {
                        actor: actor.clone(),
                        call: ProposedCall {
                            tool: CONTROL_TOOL.into(),
                            cwd: None,
                            arguments: serde_json::value::to_raw_value(&args)
                                .map_err(|_| RuntimeError::InvalidRequest)?,
                        },
                        call_id: None,
                        spawn: None,
                        prompt: None,
                        ruling: Some(resolve.ruling),
                    },
                )
                .await;
                if gate.decision != HookDecision::PassControl {
                    return Err(RuntimeError::RuntimeUnavailable);
                }
                match runtime
                    .execute_embedded_remedy(
                        &actor,
                        serde_json::from_value(args).map_err(|_| RuntimeError::InvalidRequest)?,
                    )
                    .await
                {
                    RemedyOutcome::Authorized { call: authorized }
                        if resolve.ruling == Ruling::Approve
                            && authorized.tool == tool
                            && authorized.cwd.is_none()
                            && serde_json::from_str::<Value>(authorized.arguments.get())
                                .ok()
                                .as_ref()
                                == Some(&call.arguments) =>
                    {
                        json!({"decision":"allow","reason":null})
                    }
                    RemedyOutcome::Declined { .. } if resolve.ruling == Ruling::Deny => {
                        json!({"decision":"deny","reason":"Denied by OpenAPPA policy."})
                    }
                    _ => return Err(RuntimeError::RuntimeUnavailable),
                }
            } else {
                json!({"decision":"human_review","reason":null,"review":{
                    "authority":authority,"fingerprint":fingerprint,"description":review[0].text
                }})
            }
        }
        HookDecision::DenyCall { .. } => return Err(RuntimeError::UnsupportedRemedy),
        // Operational errors are Refuse, not DenyCall (upstream hooks::fold).
        _ => return Err(RuntimeError::RuntimeUnavailable),
    };
    Ok(decision)
}
#[tokio::main]
async fn main() {
    if std::env::args().nth(1).as_deref() == Some("--annotate-blocked") {
        // Constant annotation, not an allow/deny decision or a tool-name rule.
        let mut ignored = String::new();
        let _ = io::stdin()
            .take(2 * 1024 * 1024)
            .read_to_string(&mut ignored);
        println!(
            "{}",
            json!({"version":1,"answer":{"delta":{},"requires":{"history":[],"attention":["blocked"]},"emits":[]}})
        );
        return;
    }
    let mut body = String::new();
    let response = match io::stdin().take(2 * 1024 * 1024).read_to_string(&mut body) {
        Ok(_) => match serde_json::from_str::<Request>(&body) {
            Ok(input) => match evaluate(input).await {
                Ok(decision) => json!({"version":3,"result":decision}),
                Err(code) => json!({"version":3,"error":code}),
            },
            Err(_) => json!({"version":3,"error":RuntimeError::InvalidRequest}),
        },
        Err(_) => json!({"version":3,"error":RuntimeError::InvalidRequest}),
    };
    println!("{response}");
}

#[cfg(test)]
mod tests {
    use super::*;
    const POLICY: &str = include_str!("../tests/baseline.toml");
    fn input(policy: &str, name: &str) -> Request {
        Request {
            version: 3,
            policy: policy.into(),
            resolve: None,
            request: Call {
                root_id: "context-1".into(),
                invocation_id: "event-1".into(),
                agent_id: "application".into(),
                tool_kind: "builtin".into(),
                tool_name: name.into(),
                arguments: json!({}),
                runtime: "cma".into(),
            },
        }
    }
    fn email_input(arguments: Value) -> Request {
        let mut request = input(
            include_str!("../tests/email-recipient-review.toml"),
            "mcp/mail/send_email",
        );
        request.request.tool_kind = "mcp".into();
        request.request.arguments = arguments;
        request
    }
    #[tokio::test]
    async fn email_internal_recipients_are_allowed() {
        let request = email_input(json!({
            "to": ["dana@acme.com", "lee@acme.com"], "subject": "Update", "body": "Hello"
        }));
        assert_eq!(evaluate(request).await.unwrap()["decision"], "allow");
    }
    #[tokio::test]
    async fn email_mixed_recipients_require_human_review() {
        let request = email_input(json!({
            "to": ["dana@acme.com", "pat@partner.io"], "subject": "Update", "body": "Hello"
        }));
        assert_eq!(evaluate(request).await.unwrap()["decision"], "human_review");
    }
    #[tokio::test]
    async fn email_external_recipient_requires_human_review() {
        let request = email_input(json!({
            "to": ["pat@partner.io"], "subject": "Update", "body": "Hello"
        }));
        assert_eq!(evaluate(request).await.unwrap()["decision"], "human_review");
    }
    #[tokio::test]
    async fn email_lookalike_domain_requires_human_review() {
        let request = email_input(json!({
            "to": ["x@acme.com.evil.io"], "subject": "Update", "body": "Hello"
        }));
        assert_eq!(evaluate(request).await.unwrap()["decision"], "human_review");
    }
    #[tokio::test]
    async fn email_string_recipients_are_denied_by_schema() {
        let request = email_input(json!({
            "to": "pat@partner.io, dana@acme.com", "subject": "Update", "body": "Hello"
        }));
        assert_eq!(evaluate(request).await.unwrap()["decision"], "deny");
    }
    #[tokio::test]
    async fn email_undeclared_cc_is_denied_by_schema() {
        let request = email_input(json!({
            "to": ["dana@acme.com"], "cc": ["pat@partner.io"],
            "subject": "Update", "body": "Hello"
        }));
        assert_eq!(evaluate(request).await.unwrap()["decision"], "deny");
    }
    #[tokio::test]
    async fn recipient_selector_does_not_parse_addresses() {
        // Known hole: selectors match strings; the mail tool must enforce one address per element.
        let request = email_input(json!({
            "to": ["pat@partner.io, dana@acme.com"], "subject": "Update", "body": "Hello"
        }));
        assert_eq!(evaluate(request).await.unwrap()["decision"], "allow");
    }
    #[tokio::test]
    async fn email_human_review_authorizes_exact_recipients_and_rejects_changes() {
        let arguments = json!({
            "to": ["pat@partner.io"], "subject": "Update", "body": "Hello"
        });
        let review = evaluate(email_input(arguments.clone())).await.unwrap();
        assert_eq!(review["decision"], "human_review");
        assert_eq!(review["review"]["authority"], "user");
        for (ruling, decision) in [(Ruling::Approve, "allow"), (Ruling::Deny, "deny")] {
            let mut request = email_input(arguments.clone());
            request.resolve = Some(Resolution {
                fingerprint: review["review"]["fingerprint"].as_str().unwrap().into(),
                authority: "user".into(),
                ruling,
            });
            assert_eq!(evaluate(request).await.unwrap()["decision"], decision);
        }
        let mut changed = email_input(arguments);
        changed.request.arguments["to"] = json!(["pat@partner.io", "leak@rival.com"]);
        changed.resolve = Some(Resolution {
            fingerprint: review["review"]["fingerprint"].as_str().unwrap().into(),
            authority: "user".into(),
            ruling: Ruling::Approve,
        });
        assert_eq!(
            evaluate(changed).await.unwrap_err(),
            RuntimeError::StaleReview
        );
    }
    #[tokio::test]
    async fn native_human_review_reconstructs_and_authorizes_exact_call() {
        let policy = include_str!("../tests/human-review.toml");
        let review = evaluate(input(policy, "web_search")).await.unwrap();
        assert_eq!(review["decision"], "human_review");
        assert!(
            review["review"]["description"]
                .as_str()
                .unwrap()
                .contains("user-approval")
        );
        for (ruling, decision) in [(Ruling::Approve, "allow"), (Ruling::Deny, "deny")] {
            let mut request = input(policy, "web_search");
            request.resolve = Some(Resolution {
                fingerprint: review["review"]["fingerprint"].as_str().unwrap().into(),
                authority: "user".into(),
                ruling,
            });
            assert_eq!(evaluate(request).await.unwrap()["decision"], decision);
        }
        for changed in ["arguments", "policy", "authority", "invocation"] {
            let mut request = input(policy, "web_search");
            request.resolve = Some(Resolution {
                fingerprint: review["review"]["fingerprint"].as_str().unwrap().into(),
                authority: "user".into(),
                ruling: Ruling::Approve,
            });
            match changed {
                "arguments" => request.request.arguments = json!({"new":"call"}),
                "policy" => request.policy.push_str("\n# changed"),
                "authority" => request.resolve.as_mut().unwrap().authority = "other".into(),
                "invocation" => request.request.invocation_id = "other".into(),
                _ => unreachable!(),
            }
            assert_eq!(
                evaluate(request).await.unwrap_err(),
                RuntimeError::StaleReview
            );
        }
    }
    #[tokio::test]
    async fn nonhuman_and_multiple_authority_offers_are_not_overridable() {
        let policy = include_str!("../tests/human-review.toml");
        let nonhuman = policy.replace("builtin = \"hitl\"", "builtin = \"approve\"");
        let multiple = format!(
            "{policy}\n[[policy.authority]]\nname = 'other'\nhint = 'Other reviewer'\npermits = {{attention = ['user-approval']}}\n[externals.authorities.other]\nbuiltin = 'hitl'\n"
        );
        for candidate in [nonhuman, multiple] {
            assert_eq!(
                evaluate(input(&candidate, "web_search")).await.unwrap_err(),
                RuntimeError::UnsupportedRemedy
            );
        }
    }
    #[tokio::test]
    async fn independent_calls_allow_known_tools_and_deny_unknown() {
        // Reuse identities deliberately: no receipts, history or blocked marks leak.
        for (name, decision) in [
            ("web_fetch", "allow"),
            ("unwritten", "deny"),
            ("web_search", "allow"),
        ] {
            assert_eq!(
                evaluate(input(POLICY, name)).await.unwrap()["decision"],
                decision
            );
        }
    }
    #[tokio::test]
    async fn every_call_uses_current_policy() {
        let changed = POLICY.replace("web_search", "another_tool");
        for (policy, decision) in [
            (POLICY, "allow"),
            (changed.as_str(), "deny"),
            (POLICY, "allow"),
        ] {
            assert_eq!(
                evaluate(input(policy, "web_search")).await.unwrap()["decision"],
                decision
            );
        }
    }
    #[tokio::test]
    async fn rejects_stateful_contracts_before_evaluation() {
        for (from, to) in [
            ("context_control = false", "context_control = true"),
            (
                "confined_results = []",
                "confined_results = [\"host/cma/web_fetch\"]",
            ),
            (
                "provider_run_tools = []",
                "provider_run_tools = [\"host/cma/web_fetch\"]",
            ),
            ("delta = {}", "delta = {trust = \"untrusted\"}"),
            ("delta = {}", "effects = [\"read\"]"),
            (
                "delta = {}",
                "requires = {effects = {contains = [\"read\"]}}",
            ),
            (
                "delta = {}",
                "requires = {effects = {excludes = [\"read\"]}}",
            ),
            ("effects = []", "effects = [\"read\"]"),
            ("effects = []", ""),
            ("ranks = []", "ranks = [\"untrusted\"]"),
            ("ranks = []", ""),
            ("audiences = []", "audiences = [\"self\"]"),
            ("audiences = []", ""),
        ] {
            assert_eq!(
                evaluate(input(&POLICY.replace(from, to), "web_fetch"))
                    .await
                    .unwrap_err(),
                RuntimeError::UnsupportedPolicy
            );
        }
        for extra in [
            "\n[[policy.authority]]\nname = 'history'\npermits = {effects_containing = ['read']}\n",
            "\n[[policy.sanitizer]]\nname = 'sanitize'\non = ['host/cma/web_fetch']\npermits = {trust = {from = 'untrusted', to = 'trusted'}}\n",
        ] {
            assert_eq!(
                evaluate(input(&format!("{POLICY}{extra}"), "web_fetch"))
                    .await
                    .unwrap_err(),
                RuntimeError::UnsupportedPolicy
            );
        }
    }
    #[tokio::test]
    async fn invalid_policy_and_calls_are_operational_errors() {
        assert_eq!(
            evaluate(input("broken toml", "web_fetch"))
                .await
                .unwrap_err(),
            RuntimeError::InvalidPolicy
        );
        for field in [
            "version",
            "runtime",
            "kind",
            "name",
            "arguments",
            "root",
            "event",
            "agent",
        ] {
            let mut request = input(POLICY, "web_fetch");
            match field {
                "version" => request.version = 1,
                "runtime" => request.request.runtime = "other".into(),
                "kind" => request.request.tool_kind = "other".into(),
                "name" => request.request.tool_name = "".into(),
                "arguments" => request.request.arguments = json!([]),
                "root" => request.request.root_id.clear(),
                "event" => request.request.invocation_id.clear(),
                "agent" => request.request.agent_id.clear(),
                _ => unreachable!(),
            }
            assert_eq!(
                evaluate(request).await.unwrap_err(),
                RuntimeError::InvalidRequest
            );
        }
    }
}
