# **Token Spend Control for Enterprise AI Agents** {#token-spend-control-for-enterprise-ai-agents}

## **Report to Management \- Customer Needs and Recommended Actions** {#report-to-management---customer-needs-and-recommended-actions}

*July 2026*

*Sveta Morag*

[**Token Spend Control for Enterprise AI Agents	1**](#token-spend-control-for-enterprise-ai-agents)

[Report to Management \- Customer Needs and Recommended Actions	1](#report-to-management---customer-needs-and-recommended-actions)

[2\. Scope and constraints	2](#2.-scope-and-constraints)

[3\. AI provider customer needs	3](#3.-ai-provider-customer-needs)

[4\. The consumption-plane taxonomy	3](#4.-the-consumption-plane-taxonomy)

[5\. The enforcement problem: hard cutoffs and bounded burn	4](#5.-the-enforcement-problem:-hard-cutoffs-and-bounded-burn)

[6\. Estimating agent run cost: the session/run level, not the call level	5](#6.-estimating-agent-run-cost:-the-session/run-level,-not-the-call-level)

[7\. How this is handled on other providers	5](#7.-how-this-is-handled-on-other-providers)

[8\. Google Cloud: the primary control plane \- the gap and next steps	6](#8.-google-cloud:-the-primary-control-plane---the-gap-and-next-steps)

[8.1 What Google Cloud have today	6](#8.1-what-google-cloud-have-today)

[9\. Sources	8](#9.-sources)

**1\. Executive summary**

Enterprise customers are hitting a wall on token cost control, and it is becoming a sales and retention issue, not a support issue. Per-token prices fall while invoices rise; 73% of enterprises report AI costs exceeding expectations (State of FinOps 2026). Customers accept the spend \- what they cannot accept is spend that is unattributable, unlimitable, and unpredictable.

Three findings drive this report:

1. **Customers cannot enforce spend limits per user, agent, or project on our platform.** Quotas, budgets, and Provisioned Throughput are all project-scoped; project-per-user is not viable at enterprise scale. There is no per-principal quota and no per-principal spend cap. Parallel agents can burn thousands of dollars in minutes with no platform-side stop.  
2. **The platform cannot tell customers tokens-per-principal.** Audit logs identify who called which model, but carry no token counts. Monitoring metrics carry token counts, but no principal. Customers are forced to build fragile client-side telemetry to answer "who spent this.". However, they can’t always do that successfully for third-party applications, like Claude Code, Antigravity tools and others.  
3. **Any control that requires each developer to implement something fails at fleet scale.** Per-request headers (e.g., PT spillover opt-out), in-agent budgets, and hand-added instrumentation cannot be mandated across heterogeneous tools (Claude Code, frameworks, third-party proxies). Enforcement must be involuntary: server-side, identity-scoped.

If Google Cloud implements native per-principal quotas and hard stops, it will achieve a first-mover advantage.

Next steps: close the granularity gap with two product moves (granular quotas and ADK/Agent Runtime run-level visibility \- already begun in adk-python \#6357 issue), while Customer Engineering ships guides and reference pipelines that give customers workable control today. Details in §8.

---

## **2\. Scope and constraints** {#2.-scope-and-constraints}

**In scope:** direct token consumption only \- prediction, attribution, and hard enforcement. Out of scope: subscriptions, caching costs,  infrastructure, additional agentic services and solutions.

**Customer-side constraints validated in the field:**

* Two agent classes with different billing physics: autonomous agents (per-token) and developer/researcher assistants (per-token via Agent Platform, or subscription seats).  
* A universal customer-built API gateway is infeasible \- not for protocol reasons, but because connection logic is embedded in third-party tools (Claude Code, packaged apps) and agent frameworks (ADK, Claude Agent SDK, LangGraph, LangChain) that cannot all be redeveloped at once; other workloads already sit behind proxies like LiteLLM.  
* Project-per-developer / project-per-agent is not viable, so project-scoped controls are structurally too coarse.  
* Controls that live inside individual requests or individual agents are not controls: they cannot be forced onto every developer.

---

## **3\. AI provider customer needs** {#3.-ai-provider-customer-needs}

Three needs, held in different combinations across finance, team leads, and platform teams \- role assignment varies by customer and must not be hard-wired:

| Need | Requirement | Data profile |
| ----- | ----- | ----- |
| **Attribution** | Every dollar mapped to department / team / project / user / agent | Complete and correctly labeled; month-end is fine |
| **Limiting** | Ceilings per user / agent / project, physically enforced \- not alerted | Hard, server-side, cannot be bypassed by the caller |
| **Monitoring** | Near-real-time usage per scope; anomaly detection | Fast, scoped, token-grain |

---

## **4\. The consumption-plane taxonomy** {#4.-the-consumption-plane-taxonomy}

Customer token traffic flows through four planes; each demands its own enforcement point:

| Plane | Example | Why one gateway can't cover it | Where control should live |
| ----- | ----- | ----- | ----- |
| P1 \- Direct API, framework agents | ADK / LangGraph / Claude Agent SDK pipelines | Frameworks embed provider clients; proxy redirection unsupported or inconsistent | GCP-native and runtime controls |
| P2 \- Cloud model platform | Claude Code on Agent Platform Model Garden | Connectivity is inside the third-party tool; customers can't force redevelopment | GCP-native controls |
| P3 \- Existing proxies | LiteLLM estates | Proxy is already the enforcement point | The proxy's own budgets \- add some latency to the pipeline |
| P4 \- Subscription assistants | Seat-based Claude Code, Copilot | No per-token traffic to intercept | Seat \+ overage management |

Conclusion customers reach: enforcement must be federated per plane; only the data centralizes. For P1 and P2 \- the majority of Agent Platform traffic \- the only viable enforcement point is ours.

---

## **5\. The enforcement problem: hard cutoffs and bounded burn** {#5.-the-enforcement-problem:-hard-cutoffs-and-bounded-burn}

Monitoring is not control. A limit is real only if the next over-limit request is rejected or de-authorized. 

On our platform today:

| Mechanism | Reaction | Verdict |
| ----- | ----- | ----- |
| Project quota (RPM/TPM) | Instant | Real, but shared by the whole project \- no principal grain |
| Provisioned Throughput | Instant | **Not a spend limit**: over-quota requests spill to pay-as-you-go by default; the only opt-out is a per-request header \- per-developer, therefore unenforceable |
| IAM revocation (customer-built automation) | Minutes | The only per-principal hard stop available \- customers must build it themselves |
| Billing budgets → Pub/Sub | Hours | Backstop only; billing lag defeats the minutes-scale scenario |

Two working principles from the field: billing-based controls are backstops; and while a cutoff has latency, a rate cap bounds the worst case (max overshoot \= rate × latency). The third principle is the hard one for us: enforcement must be involuntary \- anything requiring developer cooperation will be skipped by exactly the workload that causes the incident.

---

## **6\. Estimating agent run cost: the session/run level, not the call level** {#6.-estimating-agent-run-cost:-the-session/run-level,-not-the-call-level}

Everything customers want \- budgets, chargeback, "agent as a worker with billable hours" \- is a per-**run** question, and call-level data cannot answer it: context accumulation makes late calls dearer, caching makes per-call costs non-additive, reasoning tokens hide inside calls, retries double-count. Caching (e.g., Prompt Caching in Gemini) makes late calls cheaper but cost estimation harder. The needed unit is tokens-per-run as a *distribution* (P50 plan / P95 reserve / P99 ceiling), which requires a run ID on every call, all token species summed per run, and failed runs included.

**ADK is already moving on this.** Open RFC [google/adk-python\#6357](https://github.com/google/adk-python/issues/6357) proposes exactly the right seam for the optimizer surface: a run-scoped context with a per-call usage ledger (prompt / output / reasoning / cached / tool-use tokens with honest coverage classification), atomic hard limits on logical calls, reported-token budget stops, cooperative cancellation, and capability declarations so governance wrappers can fail preflight instead of discovering opacity after spend. It is assigned and under review. 

---

## **7\. How this is handled on other providers** {#7.-how-this-is-handled-on-other-providers}

Every competitor offers a server-side container with limits; none offers per-user/per-agent spend caps \- the gap is industry-wide, which makes it winnable:

* **Anthropic / OpenAI (direct API):** workspaces / projects with server-side spend limits and usage APIs; granularity \= the container, not the user.  
* **AWS Bedrock:** the reference point on attribution \- inference profiles carry cost-allocation tags into billing regardless of caller, and invocation logging captures per-call token counts server-side, with the principal. Per-principal metering is buildable from platform logs alone \- the capability Agent Platform lacks. No native per-user spend limit either.

Bedrock's server-side token logging and Azure's server-side token quotas each cover one half of what customers need. Whoever ships both halves at principal granularity first owns the enterprise cost-governance narrative.

---

## **8\. Google Cloud: the primary control plane \- the gap and next steps** {#8.-google-cloud:-the-primary-control-plane---the-gap-and-next-steps}

**The gap, stated once:** quotas, budgets, and PT are project-scoped; audit logs have principals but no tokens; metrics have tokens but no principals; PT silently spills to PAYG with only a per-request opt-out. Customers therefore cannot attribute, limit, or predict at the grain they manage people and agents \- and are hand-building IAM-revocation kill switches to compensate.

### **8.1 What Google Cloud have today** {#8.1-what-google-cloud-have-today}

The building blocks exist; none crosses the principal-grain line on its own. Stated honestly, with each capability's limitation:

| Capability today | What it gives | Limitation |
| ----- | ----- | ----- |
| Request-level labels — developers can attach labels per request; ADK agents emit labels automatically | Cost breakdown by label visible in the Billing console | Not carried into the official billing report sent by email — the artifact finance actually consumes; developer-added labels are voluntary (Principle C), so coverage is partial by construction; automatic ADK labels cover only ADK workloads |
| Tokens-per-request in responses — every direct-call response and ADK agent response carries usage metadata (input / output / thinking / cached counts) | Exact token data exists at the source for every call | Delivered to the *client* and not retained by the platform per principal — capture depends on client-side telemetry, which is uneven across the tool estate |
| Budget alerts — Cloud Billing budgets with thresholds, email and Pub/Sub notification, programmatic hooks | Awareness and automation triggers | Notification, not enforcement; billing lag of hours defeats the minutes-scale runaway scenario |
| Project quotas (per model, per region) | Instant, involuntary rate caps | Shared by the whole project — no principal grain |
| Cloud Monitoring token metrics; PT dashboards \+ recommended alerts | Near-real-time token throughput, spillover visibility | Aggregated per project/model — no principal dimension |
| Data Access audit logs | Every invocation with the calling principal, model, timestamp | No token counts, no costs, no payloads |
| BigQuery billing export; request-response logging (per endpoint, where supported) | Queryable spend; server-side payloads incl. usage metadata | Billing has no principal; request-response logging is per-endpoint with partner-model coverage to verify |
| Semantic Governance Policies (Preview, Agent Platform) with Agent Gateway and per-agent Agent Identity | Declarative natural-language constraints enforced at runtime on every proposed tool call, scoped per agent and per tool — Google's own examples include enforcing strict financial limits; deny-by-default gateway for unregistered agents/tools | May help, but the rules are deterministic statements gating kinds of actions, not amounts consumed: no cumulative spend state across a run or a month, so "stop this agent after $500" is not expressible; the enforcement engine is itself LLM-based and, per our own docs, verdicts may not be accurate; requires Agent Gateway \+ Agent Identity \+ platform runtime — covers platform-built agents only, not Claude Code, LiteLLM estates, or arbitrary framework agents |
| Centrally managed client telemetry in some tools (e.g., Claude Code managed-settings OTel) | Per-user token/cost metrics without developer action | Tool-specific; coverage equals device management, not the platform |

Read as a whole: the raw material for measurement is largely present. The gaps are **joins** (tokens ↔ principal), **surfacing** (labels into the official billing report, not just the console), and **enforcement** (alerts into hard stops). 

---

## **9\. Sources** {#9.-sources}

* FinOps Foundation — *FinOps for AI: Tools & Services Considerations*; *Token Economics* series (agent cost multipliers, gateway budget guardrails, FOCUS)  
* F. Ferreira (Medium) — *How We Built an Enterprise-Grade Claude Code Gateway on GCP* (case study: Vertex routing, BigQuery usage capture, automated spend limits)  
* Anthropic — *Claude Code on Google Cloud's Agent Platform (Vertex AI)*, code.claude.com/docs/en/google-vertex-ai (project-per-deployment guidance, IAM roles, quotas, model pinning)  
* hidekazu-konishi.com — *Claude Code on Pay-As-You-Go API Billing: Anthropic API, Bedrock, Vertex AI* (route-native spend controls; seats-vs-metered decision framework)  
* Tygart Media — *Claude on GCP: Billing, IAM, and Quota Setup for Teams* (project structure, budget Pub/Sub automation, quota management)  
* Google Cloud Community (Medium) — Claude Code \+ Vertex AI setup guides (quota mechanics, /setup-vertex, model pinning)  
* Finout — *5 Open-Source Tools to Control Your AI API Costs at the Code Level* (LiteLLM max\_budget/budget\_duration hard rejection)  
* Maxim AI — gateway and monitoring tool comparisons (hierarchical budgets; Bedrock/Azure/Vertex native-control gaps)  
* Digital Applied / DevToolLab — prompt caching engineering guides 2026, incl. arXiv 2601.06007 (41–80% agent-session savings)


