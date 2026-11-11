# Presentation

## Presentation objective

Show a working path from software inventory to a donor-reviewed proposal and PayPal Sandbox orchestration. Make the system boundaries as visible as its features: the AI interprets evidence, deterministic services calculate allocations, and the donor explicitly initiates order, capture, and payout actions.

## Elevator pitch

> Dividend turns a Debian package inventory into an explainable open-source funding proposal. It connects packages to upstream projects, surfaces the evidence behind a maintainer attribution, calculates allocations with deterministic rules, and lets a donor walk through separately approved PayPal Sandbox payment steps.

## Problem and audience

Donors often see the packages they use without seeing a clear path from a Debian source package to an upstream project, evidence, recipient eligibility, and a traceable payment. Dividend is aimed first at donors and operators who want a structured funding review. Maintainers are affected participants in identity, opt-in, evidence, and payout handling.

## Solution and simple architecture

The browser submits a package inventory to a FastAPI service. The service maps sources using structured facts and explicit overrides, then uses a deterministic allocation policy to prepare a hashed proposal. An optional Modal/DeepSeek review summarizes a constrained evidence set but cannot alter amounts. A separate payment service talks to PayPal Sandbox and records provider-confirmed states.

In this repository checkout, normal fixture discovery is repeatable and the dedicated synthetic demo gives the most reliable positive payable line. The synthetic lane contains fictional contributors and evidence; it is an orchestration demonstration, not proof of real maintainer eligibility.

## Demo prerequisites and honest setup

Before recording:

1. Start the app with PayPal Sandbox mode and valid Sandbox REST app credentials. The current `render.yaml` uses `PAYMENTS_MODE=fake`; use the Compose workflow configured for PayPal Sandbox for this demonstration, or explicitly change and review the Render configuration before deploying.
2. Configure a Sandbox business seller app, a Sandbox personal payer, and a Sandbox recipient that matches the appropriate recipient configuration. Keep credentials out of screen recordings.
3. Configure Modal tokens only if demonstrating a live AI response; otherwise describe the feature and show the disabled/unavailable state accurately.
4. Create a normal fixture-backed demo plan for the optional AI segment. Its bundled curl identity mapping is explicitly synthetic, so say that the review covers fixture evidence and does not verify a real person's identity.
5. Make sure the synthetic recipient configuration is configured for the separate synthetic workflow. Confirm it remains a Sandbox recipient.
6. Keep the database in demo state and avoid re-seeding or resetting it during the recording.

The synthetic allocation screen intentionally hides AI review. Demonstrate AI, if enabled, on the separate normal fixture-backed plan, then use **Create Synthetic Sandbox Demo** for the payable Sandbox lifecycle. If the Sandbox provider is slow or a payout item remains pending/unclaimed, show the real state and explain it; do not narrate an unconfirmed payout as successful.

## Suggested live sequence

1. Open Dividend and identify the package-to-funding workflow.
2. Create a demo package session or upload a prepared `dpkg-query` report.
3. Show mapped and unresolved packages in discovery.
4. Open the normal fixture plan and request AI Evidence Review if enabled. Show citations and state that evidence/identity mapping is fixture-based.
5. Show the deterministic allocation, project reasons, payable amount, and reserve.
6. Return to the overview and select **Create Synthetic Sandbox Demo**. Pause on the persistent fictional-evidence warning.
7. Show its positive Sandbox-payable project, recipient mapping status, reserved amount, and proposal snapshot state.
8. Select **Create PayPal order**; open the approval URL and approve using a Sandbox personal buyer account.
9. Return to Dividend and explicitly select **Confirm and capture funds**.
10. After funding is confirmed, review the payout preview and explicitly submit payout. Show provider batch/item states and workspace activity; describe pending/exception states accurately.
11. Close by separating the repeatable Sandbox demo from future real-world eligibility and production use.

## Demo script: under three minutes

| Time | Screen and action | Suggested narration | Criterion |
|---|---|---|---|
| 0:00–0:15 | Overview page. Point to the package-to-funding steps. | “Open-source dependencies are visible to users, but connecting a package inventory to upstream projects, contribution evidence, and a transparent payment plan takes several separate steps.” | Impact, presentation |
| 0:15–0:32 | Choose demo packages or load a prepared package scan and open discovery. | “Dividend starts from Debian source packages, groups them under upstream projects, and keeps unresolved sources visible.” | Implementation, design |
| 0:32–0:48 | Show a mapped project and an unmapped row. | “The mapping comes from structured facts and overrides. Unmapped entries stay out of funding until there is enough information.” | Innovation, trust |
| 0:48–1:05 | On a separate fixture plan, open Evidence Review if configured. | “This optional DeepSeek review summarizes cited contribution evidence. The output is advisory; it cannot change the allocation. This bundled identity mapping is synthetic, not verified identity.” | AI implementation, honesty |
| 1:05–1:23 | Show the normal plan's project lines, reasons, payable/reserved amounts. | “The allocation is deterministic. Each project total is split into payable and reserved cents, and the proposal is stored with a hash.” | Implementation, design |
| 1:23–1:42 | Start **Create Synthetic Sandbox Demo**; pause on warning and positive payable line. | “For a predictable payment walkthrough, this separate fixture uses fictional contributors and evidence. It never creates real maintainer claims or real eligibility.” | Innovation, safety |
| 1:42–2:00 | Funding page; create order, show PayPal Sandbox approval. | “Creating an order is not a capture. The payer approves it in PayPal Sandbox using a test buyer account.” | PayPal integration |
| 2:00–2:17 | Return and explicitly confirm capture. | “Dividend waits for a separate donor action to capture, then checks the provider's captured amount and currency.” | Payment safeguards |
| 2:17–2:42 | Open payout preview; explicitly submit and show returned batch/item state. | “Payout is another explicit operation. The app tracks each item, so a successful batch does not imply every recipient was paid.” | PayPal, auditability |
| 2:42–2:58 | Overview/activity and synthetic label. | “This proves a reproducible Sandbox orchestration path. It is not a claim of verified real-world contributor eligibility or a production payment deployment.” | Impact, presentation |

The timeline assumes the app is already running, the plan is created, AI is warmed/configured, and the Sandbox approval flow responds quickly. A live order/payout can take longer; trim earlier screens rather than skipping payer approval, explicit capture, or the synthetic disclosure.

## AI and PayPal disclosures for the video

- AI is optional, bounded, and only interprets validated evidence attached to the plan snapshot. It does not decide amounts, verify identity, or execute payments.
- Synthetic contributor labels/evidence are fictional and visibly labelled. The bundled normal demo also uses a synthetic GitHub identity mapping.
- PayPal uses the Sandbox endpoints and test accounts. The Sandbox does not move real money.
- Order creation, payer approval, capture, and payout are separate states. The user explicitly initiates capture and payout.
- Do not claim a payout succeeded unless provider reconciliation confirms the relevant item status. Pending, unclaimed, returned, failed, and unknown are distinct outcomes.
- The current Render manifest selects a fake provider; a Render deployment from it does not perform this PayPal Sandbox flow as written.

## Closing statement

> Dividend makes the path from software use to a funding decision easier to inspect. It keeps AI in an advisory role, allocation rules deterministic, uncertain amounts reserved, and Sandbox payment steps under explicit donor control.
