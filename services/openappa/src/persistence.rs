//! Reopen native trajectories and native operation receipts; never synthesize policy facts.
use super::*;
use appa_eventlog::{OperationClaim, OperationKey, OperationRequest, ReceiptBinding, SessionScope};
use appa_runtime::api::{OfferId, OfferKind};

fn operation(input: &Request, stage: &str, fingerprint: &str, extra: Value) -> OperationRequest {
    OperationRequest {
        key: OperationKey {
            session: SessionScope {
                organization_id: input.request.agent_id.clone(),
                session_id: input.request.root_id.clone(),
            },
            binding: ReceiptBinding::Session { caller_id: None },
            operation_id: format!("cma:{stage}:{}:{fingerprint}", input.request.invocation_id),
        },
        root: TrajectoryId(input.request.root_id.clone()),
        input: extra,
        context: None,
    }
}

fn saved(store: &LogStore, operation: OperationRequest) -> Result<Value, RuntimeError> {
    match store.claim_operation(operation) {
        Ok(OperationClaim::Complete { decision }) => Ok(decision),
        _ => Err(RuntimeError::StaleReview),
    }
}

fn public(mut value: Value) -> Value {
    if let Some(object) = value.as_object_mut() {
        object.remove("offer");
    }
    value
}

async fn remedy(
    runtime: &Runtime,
    actor: &Actor,
    offer: &str,
    ruling: Option<Ruling>,
) -> Result<RemedyOutcome, RuntimeError> {
    let args = json!({"offer_id":offer});
    let gate = hooks::handle_embedded(
        runtime,
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
            ruling,
        },
    )
    .await;
    if gate.decision != HookDecision::PassControl {
        return Err(RuntimeError::StaleReview);
    }
    Ok(runtime
        .execute_embedded_remedy(
            actor,
            serde_json::from_value(args).map_err(|_| RuntimeError::InvalidRequest)?,
        )
        .await)
}

fn exact_call(call: &ProposedCall, proposed: &ProposedCall) -> bool {
    call.tool == proposed.tool
        && call.cwd == proposed.cwd
        && serde_json::from_str::<Value>(call.arguments.get()).ok()
            == serde_json::from_str::<Value>(proposed.arguments.get()).ok()
}

fn resolution_input(input: &Request, fingerprint: &str) -> Value {
    json!({"fingerprint":fingerprint,"ruling":input.resolve.as_ref().map(|r|r.ruling),
        "review":input.resolve.as_ref().map(|r|r.fingerprint.as_str())})
}

pub(super) async fn evaluate(
    input: &Request,
    config: Config,
    database: &str,
    tool: String,
    fingerprint: String,
    authority: Option<String>,
) -> Result<Value, RuntimeError> {
    let restoring = std::path::Path::new(database).exists();
    let store = Arc::new(
        LogStore::open(Backend::Sqlite {
            path: database.into(),
        })
        .map_err(|_| RuntimeError::RuntimeUnavailable)?,
    );
    let runtime = Runtime::open_with_store(config, store.clone(), None)
        .map_err(|_| RuntimeError::InvalidPolicy)?;
    let root = TrajectoryId(input.request.root_id.clone());
    let actor = Actor {
        root: root.clone(),
        child: None,
    };
    if !store
        .has_root(&root)
        .map_err(|_| RuntimeError::RuntimeUnavailable)?
    {
        if restoring || input.result.is_some() || input.resolve.is_some() {
            return Err(RuntimeError::StaleReview);
        }
        if hooks::handle_embedded(
            &runtime,
            HookEvent::SessionStart {
                root,
                principal: None,
                address: None,
                title: None,
            },
        )
        .await
        .decision
            != HookDecision::Ack
        {
            return Err(RuntimeError::RuntimeUnavailable);
        }
    }
    let proposed = ProposedCall {
        tool: tool.clone(),
        cwd: None,
        arguments: serde_json::value::to_raw_value(&input.request.arguments)
            .map_err(|_| RuntimeError::InvalidRequest)?,
    };
    let call_event = HookEvent::ToolCall {
        actor: actor.clone(),
        call: proposed.clone(),
        call_id: Some(input.request.invocation_id.clone()),
        spawn: None,
        prompt: None,
        ruling: None,
    };
    let call_operation = operation(
        input,
        "call",
        &fingerprint,
        json!({"fingerprint":fingerprint}),
    );
    let stage = if input.result.is_some() {
        "result"
    } else if input.resolve.is_some() {
        "resolve"
    } else {
        "call"
    };
    let extra = if let Some(result) = &input.result {
        json!({"fingerprint":fingerprint,"digest":format!("{:x}",Sha256::digest(result.text.as_bytes())),"is_error":result.is_error,
            "resolution":resolution_input(input, &fingerprint)})
    } else if input.resolve.is_some() {
        resolution_input(input, &fingerprint)
    } else {
        call_operation.input.clone()
    };
    let current = operation(input, stage, &fingerprint, extra);
    match store
        .claim_operation(current.clone())
        .map_err(|_| RuntimeError::StaleReview)?
    {
        OperationClaim::Complete { decision } => return Ok(public(decision)),
        OperationClaim::Claimed => (),
    }
    let mut decision = if stage == "call" {
        match hooks::handle_embedded(&runtime, call_event.clone())
            .await
            .decision
        {
            HookDecision::AllowCall { spawn: None } => json!({"decision":"allow","reason":null}),
            HookDecision::DenyCall { offers, review, .. }
                if offers.is_empty() && review.is_empty() =>
            {
                json!({"decision":"deny","reason":"Denied by OpenAPPA policy."})
            }
            HookDecision::DenyCall { offers, review, .. } => {
                let kinds: Vec<_> = offers
                    .iter()
                    .map(|offer| {
                        OfferId::parse(&offer.id)
                            .ok()
                            .and_then(|id| runtime.offer_kind(&actor.root, &id))
                    })
                    .collect();
                // A sanitizer is a native dispatch remedy, selected before execution.
                // Raw Accept/Withhold alternatives are never automatic fallbacks.
                let sanitizers: Vec<_> = kinds
                    .iter()
                    .enumerate()
                    .filter_map(|(i, kind)| {
                        matches!(kind, Some(OfferKind::Sanitizer { .. })).then_some(i)
                    })
                    .collect();
                if input.request.tool_kind == "custom"
                    && sanitizers.len() == 1
                    && review.is_empty()
                    && kinds.iter().all(|kind| {
                        matches!(
                            kind,
                            Some(
                                OfferKind::Sanitizer { .. }
                                    | OfferKind::Accept
                                    | OfferKind::Withhold
                            )
                        )
                    })
                    && offers
                        .iter()
                        .all(|o| o.returns.is_none() && o.input_sanitizer.is_none())
                {
                    let offer = &offers[sanitizers[0]];
                    match remedy(&runtime, &actor, &offer.id, None).await? {
                        RemedyOutcome::Authorized { call } if exact_call(&call, &proposed) => (),
                        _ => return Err(RuntimeError::UnsupportedRemedy),
                    }
                    if !matches!(
                        hooks::handle_embedded(&runtime, call_event.clone())
                            .await
                            .decision,
                        HookDecision::AllowCall { spawn: None }
                    ) {
                        return Err(RuntimeError::StaleReview);
                    }
                    json!({"decision":"allow","reason":null,"offer":offer.id})
                } else if offers.len() == 1
                    && review.len() == 1
                    && authority.is_some()
                    && review[0].offer == offers[0].id
                    && !review[0].text.is_empty()
                    && offers[0].returns.is_none()
                    && offers[0].input_sanitizer.is_none()
                    && matches!(&kinds[0], Some(OfferKind::Authority { names }) if names == &vec![authority.clone().unwrap()])
                {
                    let consent = format!("{:x}",Sha256::digest(serde_json::to_vec(
                        &json!({"call":fingerprint,"offer":offers[0].id,"review":review[0].text}))
                        .map_err(|_|RuntimeError::InvalidRequest)?));
                    json!({"decision":"human_review","reason":null,"offer":offers[0].id,
                        "review":{"authority":authority,"fingerprint":consent,"description":review[0].text}})
                } else {
                    return Err(RuntimeError::UnsupportedRemedy);
                }
            }
            _ => return Err(RuntimeError::RuntimeUnavailable),
        }
    } else {
        let original = saved(&store, call_operation)?;
        if let Some(resolve) = &input.resolve {
            if original["review"]["fingerprint"] != resolve.fingerprint
                || original["review"]["authority"] != resolve.authority
            {
                return Err(RuntimeError::StaleReview);
            }
        }
        if stage == "resolve" {
            let resolve = input.resolve.as_ref().ok_or(RuntimeError::StaleReview)?;
            if original["decision"] != "human_review" || !original["offer"].is_string() {
                return Err(RuntimeError::StaleReview);
            }
            match remedy(
                &runtime,
                &actor,
                original["offer"]
                    .as_str()
                    .ok_or(RuntimeError::StaleReview)?,
                Some(resolve.ruling),
            )
            .await?
            {
                RemedyOutcome::Authorized { call }
                    if resolve.ruling == Ruling::Approve
                        && call.tool == proposed.tool
                        && call.cwd.is_none()
                        && serde_json::from_str::<Value>(call.arguments.get()).ok()
                            == Some(input.request.arguments.clone()) =>
                {
                    // Bind the exact host occurrence to the now-authorized dispatch.
                    if !matches!(
                        hooks::handle_embedded(&runtime, call_event).await.decision,
                        HookDecision::AllowCall { spawn: None }
                    ) {
                        return Err(RuntimeError::StaleReview);
                    }
                    json!({"decision":"allow","reason":null})
                }
                RemedyOutcome::Declined { .. } if resolve.ruling == Ruling::Deny => {
                    json!({"decision":"deny","reason":"Denied by OpenAPPA policy."})
                }
                _ => return Err(RuntimeError::StaleReview),
            }
        } else {
            let authorized = if input.resolve.is_some() {
                saved(
                    &store,
                    operation(
                        input,
                        "resolve",
                        &fingerprint,
                        resolution_input(input, &fingerprint),
                    ),
                )?
            } else {
                original
            };
            if authorized["decision"] != "allow" {
                return Err(RuntimeError::StaleReview);
            }
            let outcome = input.result.as_ref().ok_or(RuntimeError::InvalidRequest)?;
            if outcome.is_error && outcome.text != "The custom tool failed." {
                return Err(RuntimeError::InvalidRequest);
            }
            let native = if outcome.is_error {
                ToolOutcome::Failure {
                    message: outcome.text.clone(),
                }
            } else {
                ToolOutcome::Success {
                    body: OutcomeBody::Available(outcome.text.clone()),
                }
            };
            match hooks::handle_embedded(
                &runtime,
                HookEvent::ToolResult {
                    actor,
                    call: proposed,
                    call_id: Some(input.request.invocation_id.clone()),
                    outcome: native,
                },
            )
            .await
            .decision
            {
                HookDecision::Ack => json!({"decision":"deliver","text":outcome.text}),
                HookDecision::DeliverValue { value } if value.len() <= 65536 => {
                    json!({"decision":"deliver","text":value})
                }
                HookDecision::ReplaceOutput { .. } | HookDecision::DeliverValue { .. } => {
                    json!({"decision":"withhold"})
                }
                _ => return Err(RuntimeError::RuntimeUnavailable),
            }
        }
    };
    if input.request.tool_kind == "custom" && decision["decision"] == "allow" {
        decision["fingerprint"] = json!(fingerprint);
    }
    store
        .complete_operation(current.key, decision.clone())
        .map_err(|_| RuntimeError::RuntimeUnavailable)?;
    Ok(public(decision))
}
