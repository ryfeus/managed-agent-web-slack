//! Example-only native bridge. Production's CMA adapter deliberately has a smaller remedy surface.
use crate::tools::{Call, MAX_BYTES};
use appa_eventlog::{
    Backend, LogStore, OperationClaim, OperationKey, OperationRequest, ReceiptBinding, SessionScope,
};
use appa_runtime::{
    api::{OfferId, OfferKind, RemedyOutcome, Runtime},
    config::Config,
    hooks,
};
use appa_runtime_api::{
    Actor, CONTROL_TOOL, HookDecision, HookEvent, OutcomeBody, ProposedCall, Ruling, ToolOutcome,
    TrajectoryId,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{path::PathBuf, sync::Arc};

type Result<T> = std::result::Result<T, &'static str>;
const ROOT: &str = "support-triage";

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Review {
    pub offer: String,
    pub fingerprint: String,
    pub description: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "decision", rename_all = "snake_case", deny_unknown_fields)]
pub enum Decision {
    Started,
    Allow { remedy: Option<String> },
    Deny,
    Review { review: Review },
    Deliver { text: String },
    Withhold,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "stage", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    Start,
    Call {
        call: Call,
    },
    Resolve {
        call: Call,
        review: Review,
        approve: bool,
    },
    Result {
        call: Call,
        text: String,
    },
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub database: PathBuf,
    pub policy: PathBuf,
    pub operation: Operation,
}

pub fn digest(value: &impl Serialize) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).expect("serializable data"))
    )
}

fn receipt(stage: &str, id: &str, input: Value) -> OperationRequest {
    OperationRequest {
        key: OperationKey {
            session: SessionScope {
                organization_id: ROOT.into(),
                session_id: ROOT.into(),
            },
            binding: ReceiptBinding::Session { caller_id: None },
            operation_id: format!("{stage}:{id}"),
        },
        root: TrajectoryId(ROOT.into()),
        input,
        context: None,
    }
}

fn saved(store: &LogStore, operation: OperationRequest) -> Result<Decision> {
    match store
        .claim_operation(operation)
        .map_err(|_| "receipt-mismatch")?
    {
        OperationClaim::Complete { decision } => {
            serde_json::from_value(decision).map_err(|_| "invalid-receipt")
        }
        _ => Err("receipt-missing"),
    }
}

fn complete(store: &LogStore, key: OperationKey, decision: Decision) -> Result<Decision> {
    store
        .complete_operation(
            key,
            serde_json::to_value(&decision).map_err(|_| "invalid-decision")?,
        )
        .map_err(|_| "state-unavailable")?;
    Ok(decision)
}

fn proposed(call: &Call) -> ProposedCall {
    ProposedCall {
        tool: format!("host/cma-custom/{}", call.name),
        cwd: None,
        arguments: serde_json::value::to_raw_value(&call.input).expect("JSON input"),
    }
}

fn call_event(actor: &Actor, call: &Call) -> HookEvent {
    HookEvent::ToolCall {
        actor: actor.clone(),
        call: proposed(call),
        call_id: Some(call.id.clone()),
        spawn: None,
        prompt: None,
        ruling: None,
    }
}

fn exact_call(actual: &ProposedCall, call: &Call) -> bool {
    actual.tool == proposed(call).tool
        && actual.cwd.is_none()
        && serde_json::from_str::<Value>(actual.arguments.get()).ok() == Some(call.input.clone())
}

async fn remedy(
    runtime: &Runtime,
    actor: &Actor,
    offer: &str,
    ruling: Option<Ruling>,
) -> Result<RemedyOutcome> {
    let args = json!({"offer_id":offer});
    let gate = hooks::handle_embedded(
        runtime,
        HookEvent::ToolCall {
            actor: actor.clone(),
            call: ProposedCall {
                tool: CONTROL_TOOL.into(),
                cwd: None,
                arguments: serde_json::value::to_raw_value(&args).expect("JSON input"),
            },
            call_id: None,
            spawn: None,
            prompt: None,
            ruling,
        },
    )
    .await;
    if gate.decision != HookDecision::PassControl {
        return Err("stale-offer");
    }
    Ok(runtime
        .execute_embedded_remedy(
            actor,
            serde_json::from_value(args).map_err(|_| "invalid-offer")?,
        )
        .await)
}

async fn authorize(
    runtime: &Runtime,
    actor: &Actor,
    call: &Call,
    policy_digest: &str,
) -> Result<Decision> {
    match hooks::handle_embedded(runtime, call_event(actor, call))
        .await
        .decision
    {
        HookDecision::AllowCall { spawn: None } => {
            // This demo promises masking on every log read. A changed policy must not
            // silently make an unsanitized AllowCall an alternate path.
            if call.name == "fetch_logs" {
                return Err("masker-not-selected");
            }
            Ok(Decision::Allow { remedy: None })
        }
        HookDecision::DenyCall { offers, review, .. } if offers.is_empty() && review.is_empty() => {
            Ok(Decision::Deny)
        }
        HookDecision::DenyCall { offers, review, .. } => {
            if offers
                .iter()
                .any(|o| o.returns.is_some() || o.input_sanitizer.is_some())
            {
                return Err("unsupported-remedy");
            }
            let kinds: Vec<_> = offers
                .iter()
                .map(|o| {
                    OfferId::parse(&o.id)
                        .ok()
                        .and_then(|id| runtime.offer_kind(&actor.root, &id))
                })
                .collect();
            let selected = if call.name == "read_ticket"
                && review.is_empty()
                && kinds == [Some(OfferKind::Accept)]
            {
                Some((0, "accept narrower audience"))
            } else if call.name == "fetch_logs" && review.is_empty() {
                let masks: Vec<_> = kinds.iter().enumerate().filter_map(|(i, kind)| {
                    matches!(kind, Some(OfferKind::Sanitizer { name }) if name == "mask-secrets").then_some(i)
                }).collect();
                if masks.len() == 1 && kinds.iter().all(|kind|
                    matches!(kind, Some(OfferKind::Accept | OfferKind::Withhold)) ||
                    matches!(kind, Some(OfferKind::Sanitizer { name }) if name == "mask-secrets")) {
                    Some((masks[0], "sanitizer mask-secrets"))
                } else { return Err("unsupported-remedy"); }
            } else {
                None
            };
            if let Some((index, label)) = selected {
                match remedy(runtime, actor, &offers[index].id, None).await? {
                    RemedyOutcome::Authorized { call: actual } if exact_call(&actual, call) => (),
                    _ => return Err("stale-offer"),
                }
                if !matches!(
                    hooks::handle_embedded(runtime, call_event(actor, call))
                        .await
                        .decision,
                    HookDecision::AllowCall { spawn: None }
                ) {
                    return Err("stale-offer");
                }
                return Ok(Decision::Allow {
                    remedy: Some(label.into()),
                });
            }
            if call.name == "create_issue"
                && offers.len() == 1
                && review.len() == 1
                && review[0].offer == offers[0].id
                && !review[0].text.is_empty()
                && matches!(&kinds[0], Some(OfferKind::Authority { names }) if names == &["maintainer"])
            {
                let fingerprint = digest(&json!({"policy":policy_digest,"call":call,
                    "offer":offers[0].id,"description":review[0].text,"authority":"maintainer"}));
                return Ok(Decision::Review {
                    review: Review {
                        offer: offers[0].id.clone(),
                        fingerprint,
                        description: review[0].text.clone(),
                    },
                });
            }
            Err("unsupported-remedy")
        }
        _ => Err("policy-unavailable"),
    }
}

// The example never invokes external annotators/sanitizers or credential-bearing services.
fn validate_bindings(policy: &str) -> Result<()> {
    let document: toml::Value = toml::from_str(policy).map_err(|_| "invalid-policy")?;
    let external = document
        .get("externals")
        .and_then(toml::Value::as_table)
        .ok_or("invalid-policy")?;
    for (key, value) in external {
        match key.as_str() {
            "timeout_ms" | "max_body_bytes" if value.is_integer() => (),
            "authorities" | "sanitizers" => {
                for (name, binding) in value.as_table().ok_or("unsupported-binding")? {
                    let expected = match (key.as_str(), name.as_str()) {
                        ("authorities", "maintainer") => "hitl",
                        ("sanitizers", "mask-secrets") => "redact-secrets",
                        _ => return Err("unsupported-binding"),
                    };
                    let table = binding.as_table().ok_or("unsupported-binding")?;
                    if table.len() != 1
                        || table.get("builtin").and_then(toml::Value::as_str) != Some(expected)
                    {
                        return Err("unsupported-binding");
                    }
                }
            }
            _ => return Err("unsupported-binding"),
        }
    }
    Ok(())
}

pub async fn evaluate(request: Request) -> Result<Decision> {
    let policy = std::fs::read_to_string(&request.policy).map_err(|_| "policy-unavailable")?;
    validate_bindings(&policy)?;
    let policy_digest = digest(&policy);
    let starting = matches!(request.operation, Operation::Start);
    if request.database.exists() == starting {
        return Err("state-missing-or-already-started");
    }
    let config = Config::load_from(&request.policy, &[]).map_err(|_| "invalid-policy")?;
    let store = Arc::new(
        LogStore::open(Backend::Sqlite {
            path: request.database,
        })
        .map_err(|_| "state-unavailable")?,
    );
    let actor = Actor {
        root: TrajectoryId(ROOT.into()),
        child: None,
    };
    if store
        .has_root(&actor.root)
        .map_err(|_| "state-unavailable")?
        == starting
    {
        return Err("state-missing-or-already-started");
    }
    let pin = receipt("policy", ROOT, json!({"digest":policy_digest}));
    if !starting && saved(&store, pin.clone())? != Decision::Started {
        return Err("policy-changed");
    }
    let runtime =
        Runtime::open_with_store(config, store.clone(), None).map_err(|_| "state-unavailable")?;
    if starting {
        store
            .claim_operation(pin.clone())
            .map_err(|_| "state-unavailable")?;
        if hooks::handle_embedded(
            &runtime,
            HookEvent::SessionStart {
                root: actor.root,
                principal: None,
                address: None,
                title: None,
            },
        )
        .await
        .decision
            != HookDecision::Ack
        {
            return Err("policy-unavailable");
        }
        return complete(&store, pin.key, Decision::Started);
    }
    let (stage, call) = match &request.operation {
        Operation::Call { call } => ("call", call),
        Operation::Resolve { call, .. } => ("resolve", call),
        Operation::Result { call, .. } => ("result", call),
        Operation::Start => unreachable!(),
    };
    call.prepare()?;
    let binding = json!({"policy":policy_digest,"call":call});
    let call_receipt = receipt("call", &call.id, binding.clone());
    let input = match &request.operation {
        Operation::Result { text, .. } => json!({"binding":binding,"result_digest":digest(text)}),
        _ => binding.clone(),
    };
    let operation = receipt(stage, &call.id, input);
    match store
        .claim_operation(operation.clone())
        .map_err(|_| "receipt-mismatch")?
    {
        OperationClaim::Complete { decision } => {
            return serde_json::from_value(decision).map_err(|_| "invalid-receipt");
        }
        OperationClaim::Claimed => (),
    }
    let decision = match &request.operation {
        Operation::Call { .. } => authorize(&runtime, &actor, call, &policy_digest).await?,
        Operation::Resolve {
            review, approve, ..
        } => {
            if saved(&store, call_receipt)?
                != (Decision::Review {
                    review: review.clone(),
                })
            {
                return Err("stale-review");
            }
            let id = OfferId::parse(&review.offer).map_err(|_| "stale-review")?;
            if runtime.offer_kind(&actor.root, &id)
                != Some(OfferKind::Authority {
                    names: vec!["maintainer".into()],
                })
            {
                return Err("stale-review");
            }
            match remedy(
                &runtime,
                &actor,
                &review.offer,
                Some(if *approve {
                    Ruling::Approve
                } else {
                    Ruling::Deny
                }),
            )
            .await?
            {
                RemedyOutcome::Authorized { call: actual }
                    if *approve && exact_call(&actual, call) =>
                {
                    if !matches!(
                        hooks::handle_embedded(&runtime, call_event(&actor, call))
                            .await
                            .decision,
                        HookDecision::AllowCall { spawn: None }
                    ) {
                        return Err("stale-review");
                    }
                    Decision::Allow {
                        remedy: Some("maintainer approved this call".into()),
                    }
                }
                RemedyOutcome::Declined { .. } if !approve => Decision::Deny,
                _ => return Err("stale-review"),
            }
        }
        Operation::Result { text, .. } => {
            let mut authorization = saved(&store, call_receipt)?;
            if matches!(authorization, Decision::Review { .. }) {
                authorization = saved(&store, receipt("resolve", &call.id, binding))?;
            }
            if !matches!(authorization, Decision::Allow { .. }) {
                return Err("authorization-missing");
            }
            if text.len() > MAX_BYTES {
                return Err("result-too-large");
            }
            match hooks::handle_embedded(
                &runtime,
                HookEvent::ToolResult {
                    actor,
                    call: proposed(call),
                    call_id: Some(call.id.clone()),
                    outcome: ToolOutcome::Success {
                        body: OutcomeBody::Available(text.clone()),
                    },
                },
            )
            .await
            .decision
            {
                HookDecision::Ack if call.name != "fetch_logs" => {
                    Decision::Deliver { text: text.clone() }
                }
                HookDecision::DeliverValue { value } if value.len() <= MAX_BYTES => {
                    Decision::Deliver { text: value }
                }
                HookDecision::ReplaceOutput { .. } | HookDecision::DeliverValue { .. } => {
                    Decision::Withhold
                }
                _ => return Err("result-not-admitted"),
            }
        }
        Operation::Start => unreachable!(),
    };
    complete(&store, operation.key, decision)
}
