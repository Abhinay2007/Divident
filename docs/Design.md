# Design

## Product design philosophy

Dividend treats funding as a sequence of reviewable decisions. The interface makes the source of project mappings, allocation reasons, reserved amounts, recipient totals, and payment state visible before asking a donor to take an external action. It keeps package discovery, AI review, deterministic allocation, order creation, capture, and payout as separate stages so a user can see what has happened and what remains pending.

The current UI is a working single-page application, not a static pitch mockup. Screens and controls below refer to components in `frontend/src/`.

## Users and roles

| User | What they do |
|---|---|
| Donor / operator | Imports a package inventory, reviews project mapping and funding proposal, approves an order, explicitly confirms capture, and separately requests payout. |
| Maintainer | May redeem an invitation and verify a GitHub account when GitHub OAuth is configured; eligibility also requires an active, opted-in recipient record and valid evidence. |
| Judge / demo operator | Follows a reproducible fixture workflow and can use the explicitly labelled Synthetic Sandbox Demo to inspect the PayPal Sandbox lifecycle without claiming real contributor eligibility. |

The app does not expose a general administrative console for editing all records. Administrative claim invitation uses the configured admin token.

## Information architecture

`frontend/src/App.tsx` maps browser paths to screens:

| Route | Screen | Decision shown |
|---|---|---|
| `/` | Overview and session entry | Choose demo packages, upload a scan report, paste source names, or create a Synthetic Sandbox Demo. |
| `/discovery/:sessionId` | Package discovery | See session progress, mapped upstream projects, unmapped sources, and source evidence. |
| `/allocation/:planId` | Allocation review | Inspect project totals, payable and reserved splits, reason explanations, and optional AI Evidence Review. |
| `/fund/:planId` | Funding | Create/retrieve an order, follow PayPal approval, and explicitly confirm capture. |
| `/payouts/:planId` | Payouts | Review each recipient amount and status, submit a payout, retry eligible failures, or reconcile. |
| `/claim` | Maintainer claim | Redeem/inspect a claim and continue to GitHub authorization if configured. |

`WorkspaceActivity.tsx` presents recent session and plan activity on the overview. The current UI does not render a full ledger-event explorer.

## Complete user journey

1. **Start with a package inventory.** On the overview, the donor can use the bundled demo, import a schema-1 scan report from `dpkg-query`, or paste Debian source package names. Upload and paste inputs are validated before a session is created.
2. **Review discovery.** The discovery page polls resolving sessions, displays project groupings and unresolved/no-upstream sources, and communicates whether a session is still processing or ready.
3. **Create and inspect a proposal.** The donor selects a budget and sees project totals with deterministic explanations. Payable and reserved amounts are shown separately. The proposal has a canonical frozen snapshot and hash that the backend verifies before payment.
4. **Review available contribution evidence.** A donor may request the optional AI Evidence Review when the plan has eligible evidence and the Modal provider is enabled. The AI output is advisory. The standard seeded evidence has a synthetic identity mapping and must be described as fixture evidence, not as a verified real maintainer identity.
5. **Create the funding order.** From the funding screen the donor explicitly requests a PayPal Sandbox order. The UI opens PayPal's approval link; order creation alone does not capture money.
6. **Return and explicitly capture.** After payer approval, the donor returns to the plan, checks the amount and state, then selects **Confirm and capture funds**. Capture is a separate API action.
7. **Review and explicitly request payout.** After a funded state, the payout screen shows recipients, project context, cents, item references, and statuses. The donor explicitly submits the batch. Provider results are reconciled per item; an unclaimed or unknown item is not represented as successfully paid.

## Discovery and upstream mapping experience

`Discovery.tsx` focuses the user on source-package progress and the resulting upstream groups. It exposes a session status and source count, then presents mapped project context and unmapped/no-upstream outcomes. Users can return to the overview or continue to plan creation when the session is ready. It does not pretend every Debian source can be confidently mapped: unresolved entries stay visible rather than being silently funded.

The normal demo session is fixture-backed. A real host scan is still a local `dpkg-query` report; the native launcher creates that report and asks the user to upload it. For consistent presentation, say whether the screen is showing bundled fixtures, a user-provided scan, or live enrichment.

## Allocation and proposal experience

`AllocationReview.tsx` shows a budget summary and project-level distribution, with explanation text from the policy engine. The donor can inspect the frozen proposal and access separate funding and payout pages. A control that is not supported or eligible is disabled with a reason; changing a budget creates a revised draft rather than silently changing an already frozen proposal.

The three-number payment summary communicates charged-now, pledged/reserved, and fee estimate values. A payable amount is not the same as a completed payout: capture and recipient delivery are separate operations with their own states.

## Evidence review experience

The optional review is triggered by a user from the allocation screen; it is not run during every discovery or allocation. The model summarizes a constrained set of validated contributor IDs and public PR references. The UI presents a donor-facing summary and highlights with citations. Evidence availability and validation errors remain visible as unavailable/error states. The interface should be narrated as AI-assisted interpretation of evidence supplied to the review, not an identity verification or funding decision.

## PayPal payment experience

`FundScreen.tsx` has a three-step progress display: create order, approve with PayPal, confirm capture. It distinguishes order charge from confirmed capture and actual capture data. On return from PayPal, the user must still press the explicit capture button. If the outcome is uncertain, the screen offers a status refresh/reconciliation path rather than encouraging a duplicate order or claiming completion.

`Payouts.tsx` is separate. It previews the frozen split, blocks submission if displayed recipients do not sum to the payable total, and shows batch/item results. Only failed items are retryable; pending/unclaimed/returned/successful items are not retried. The synthetic workflow also displays a persistent warning across discovery, allocation, funding, payout, and activity views.

## CLI experience

The CLI exposes two actual commands: `python -m dividend scan` and `python -m dividend configure-sandbox-recipients`. The first emits a deterministic JSON report from host `dpkg-query` data or the bundled `--demo` fixture. The second configures Sandbox recipient records in an existing SQLite database from a validated schema-2/schema-3 file. The native launcher wraps host scanning and starts both services; it does not auto-approve or capture a payment.

## Feedback, trust, and usability

The UI uses status labels, loading skeletons, empty/error states, retry controls, request IDs for API errors, semantic buttons/forms, accessible labels, and reduced-motion CSS. Session polling runs while resolving and stops when stable; payout state polls while `paying`. The payment environment is shown in the application header so the user can distinguish a fake provider from PayPal Sandbox.

Trust depends on terminology: "reserved" means not assigned to a payable maintainer line; "order created" does not mean payer approval; payer approval does not mean capture; a payout batch response does not mean every item reached a recipient. The synthetic warning states that contributor labels and evidence are fictional.

## Visual evidence and current limits

The repository has no checked-in screenshots. The components above are the verified visual references. The implementation is a compact workflow, not a polished ledger dashboard: the activity view summarizes session/plan states rather than showing every ledger event, the GitHub claim screen depends on operator-configured OAuth, and the fixed synthetic lane intentionally hides AI evidence review. Mobile/responsive and assistive-technology testing is not documented as a completed formal audit; the code includes semantic labels and reduced-motion handling but should still receive a manual accessibility pass.

## Design improvements to consider

- Add a read-only ledger timeline that links state changes to provider confirmations without implying delivery when a payout item is unclaimed or pending.
- Make evidence provenance, snapshot age, and synthetic identity mappings more prominent beside each contributor highlight.
- Add a clear guided setup for the optional OAuth, Modal, and Sandbox recipient files, with configuration validation before users enter a funding flow.
- Run repeatable keyboard, screen-reader, contrast, and mobile-layout reviews before a broad public launch.
