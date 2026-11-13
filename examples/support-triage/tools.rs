//! Mock business tools. They have no provider clients, credentials or external effects.
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const MAX_BYTES: usize = 65536;
pub const RAW_LOGS: &str =
    "T-4821 service=checkout status=503\napi_key=FAKE-support-triage-not-a-credential\nretries=3";
pub const TICKET: &str =
    "Synthetic ticket T-4821: Northwind Traders; contract tier Gold; checkout outage (HTTP 503).";

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Call {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: CallKind,
    pub name: String,
    pub input: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub enum CallKind {
    #[serde(rename = "agent.custom_tool_use")]
    Custom,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicketInput {
    pub ticket_id: TicketId,
}

#[derive(Deserialize)]
pub enum TicketId {
    #[serde(rename = "T-4821")]
    T4821,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Tracker {
    Internal,
    Public,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IssueInput {
    pub tracker: Tracker,
    pub title: String,
    pub body: String,
}

pub enum Prepared {
    Ticket(TicketInput),
    Logs(TicketInput),
    Issue(IssueInput),
}

impl Call {
    pub fn new(id: &str, name: &str, input: Value) -> Self {
        Self {
            id: id.into(),
            kind: CallKind::Custom,
            name: name.into(),
            input,
        }
    }

    pub fn prepare(&self) -> Result<Prepared, &'static str> {
        if self.id.is_empty() || self.id.len() > 128 || self.input.to_string().len() > MAX_BYTES {
            return Err("invalid-arguments");
        }
        match self.name.as_str() {
            "read_ticket" => serde_json::from_value(self.input.clone()).map(Prepared::Ticket),
            "fetch_logs" => serde_json::from_value(self.input.clone()).map(Prepared::Logs),
            "create_issue" => serde_json::from_value(self.input.clone()).map(Prepared::Issue),
            _ => return Err("unknown-tool"),
        }
        .map_err(|_| "invalid-arguments")
    }
}

impl Prepared {
    pub fn execute(self, invocation_id: &str) -> String {
        match self {
            Self::Ticket(input) => {
                let TicketId::T4821 = input.ticket_id;
                TICKET.into()
            }
            Self::Logs(input) => {
                let TicketId::T4821 = input.ticket_id;
                RAW_LOGS.into()
            }
            Self::Issue(input) => {
                // Consume only the admitted content supplied by the simulated agent.
                let _issue = (input.title, input.body);
                json!({"simulated":true,"tracker":input.tracker,"receipt":invocation_id})
                    .to_string()
            }
        }
    }
}
