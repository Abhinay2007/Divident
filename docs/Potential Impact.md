# Potential Impact

## The problem

Open-source software is commonly consumed through package managers, libraries, and transitive dependencies. The person or organization using a package may know its binary package name without knowing the upstream project, which maintainers support it, or what contribution evidence is available. A donor who wants to fund upstream work must assemble this context, decide how to attribute funds, coordinate approval and payment, and keep records of what actually happened.

These steps are often spread across package metadata, upstream project pages, contribution platforms, donation forms, and payment records. The result can be a fragmented decision process: project identity may be uncertain, contribution evidence may be incomplete, attribution may be difficult to explain, and payment intent can be mistaken for completed payment.

Dividend addresses this as a funding-orchestration problem. It does not claim that package counts equal social value, that contribution activity equals maintainer status, or that a single formula establishes the right donation.

## Stakeholders

| Stakeholder | Practical concern | Dividend feature that may help |
|---|---|---|
| Individual donor | Turn an installed dependency list into a comprehensible shortlist and allocation proposal. | Debian scan/upload/paste intake, upstream grouping, deterministic explanations, and reserve totals. |
| Organization or platform operator | Make funding decisions reproducible and preserve evidence of state changes. | Versioned allocation policy, frozen proposal hash, integer-cent accounting, and ledger events. |
| Open-source project | Be represented by the correct upstream identity and receive understandable funding context. | Source-to-upstream mapping and visible mapping/evidence outcomes. |
| Maintainer | Have identity, consent, evidence, market eligibility, and payout destination handled distinctly. | Invitation-backed GitHub claim flow, explicit opt-in record, server-side evidence validation, and separate recipient records. |
| Evaluator / auditor | Tell the difference between an AI summary, a donor decision, and a provider-confirmed transaction. | Separate evidence review, allocation, order, approval, capture, payout, and reconciliation states. |

## How the workflow addresses the problem

### Discover dependencies and upstreams

The scanner groups installed binary packages into Debian source packages. The session pipeline enriches sources with fixture or configured live UDD/GitHub facts, applies explicit family/no-upstream overrides, and groups mapped sources under upstream project keys. Unmapped sources remain visible and do not receive funding lines.

This reduces manual lookup for the package-to-project step, but coverage is not complete. Live mapping depends on the available structured facts and current overrides. The application does not currently invoke its standalone LLM upstream resolver in the session runner.

### Review evidence with appropriate limits

Plan preparation can use a recorded contribution snapshot; `EvidenceReviewService` can ask the configured Modal/DeepSeek endpoint for a concise interpretation of the frozen evidence. The AI is constrained to supplied contributor IDs and PR references and its output is validated. It does not certify a person's identity or set payment amounts.

The bundled curl identity mapping is explicitly synthetic, so it is suitable for a repeatable walkthrough only when described honestly. The dedicated synthetic demo makes the fiction unmistakable and is isolated from normal eligible-recipient records.

### Explain allocations and show uncertainty

The project allocator uses documented weights, caps, minimums, redistribution, and deterministic rounding. The proposal shows project totals, payable shares, and reserved amounts. When evidence or opt-in requirements are not met, the unsupported portion can stay reserved instead of being presented as earned by a maintainer.

This creates a testable process for a donor, not a claim that the policy captures every dimension of project value. Donors still need to inspect and decide whether the chosen policy fits their intent.

### Distinguish payment states

PayPal Sandbox order creation, payer approval, capture, payout, and payout reconciliation are separate operations. The explicit UI steps help distinguish an order from captured funds and a payout batch from successful recipient-level outcomes. State transitions and important events are persisted.

The Sandbox demonstrates integration behavior and does not move real money. A Sandbox payout response is not proof of real-world impact or a recipient's production eligibility.

## Expected benefits (hypotheses)

If validated with donors and maintainers, Dividend may:

- reduce the effort needed to map a package inventory to upstream projects;
- make funding rationale and uncertainty easier to review;
- make unsupported attribution visible through reserved funds;
- help donors distinguish AI interpretation from deterministic financial decisions;
- provide a repeatable path from proposal to provider-confirmed Sandbox operations;
- improve auditability by tying state changes to stored proposal and ledger records.

These are expected benefits of the design, not measured results. The repository contains no customer study, production usage, funded-project outcomes, or measured time savings to support stronger claims.

## Proposed impact indicators

An evaluation should gather a baseline and compare it with a Dividend-assisted workflow. The following are proposed future indicators, not currently reported project metrics:

| Indicator | Possible measurement |
|---|---|
| Upstream identification coverage | Share of submitted Debian source packages mapped to a verified/accepted upstream identity; report unmapped and rejected rows separately. |
| Proposal breadth | Number of distinct upstream projects represented in donor-reviewed proposals. |
| Evidence coverage | Share of payable allocations with eligible, cited, server-validated evidence and explicit recipient opt-in. |
| Preparation time | Time from a valid scan import to a reviewed proposal, compared with a documented manual baseline. |
| Reserve transparency | Fraction of allocation lines showing a specific reserve reason and evidence/eligibility gap. |
| Payment workflow completion | Number of Sandbox workflows completing order, payer approval, explicit capture, and payout reconciliation; report each stage separately. |
| Recipient outcomes | In a future consented real-money study, provider-confirmed recipient outcomes and exceptions, reported only after appropriate legal/operational review. |

Do not count a generated proposal as funding impact. Do not count a created payout batch as received money. A future measurement plan should report failures and unknown states, not only successful runs.

## Adoption possibilities

The current interface is centered on Debian/Ubuntu package inventories and a small fixture set. If mapping coverage, consent, security, and operational support grow, the workflow could be evaluated by individual Linux users, open-source program offices, foundations, or organizations that already have a donor-defined budget. Those are possible audiences, not current users or integrations.

The project could also be evaluated as an orchestration layer alongside existing package managers, project metadata sources, and payment systems. The codebase does not establish integrations with any foundation or service beyond the implemented UDD/GitHub and PayPal Sandbox paths.

## Risks and limitations

- **Mapping uncertainty:** a package may be misidentified, split, or maintained downstream. Unmapped entries remain unfunded, but a mapped result still requires review.
- **Evidence and identity:** public contribution activity does not prove maintainership, consent, or control of a payout destination. Seeded mappings are synthetic.
- **Policy bias:** usage and need factors can privilege observable signals and may not reflect maintainer workload, project sustainability, or donor values.
- **Privacy and consent:** identity claims and recipient email configuration require careful handling. Live evidence collection and OAuth require operator configuration.
- **AI limits:** generated prose may still be incomplete or misleading despite schema/citation checks. It is advisory and should be reviewed.
- **Payment and operations:** the current supported PayPal environment is Sandbox. SQLite is suited to the single-instance demo setup, not multi-instance concurrent production operations.
- **No demonstrated ecosystem impact:** the codebase reports no deployed user counts, total funding, time savings, or project outcomes.

## Long-term potential

Dividend's long-term value depends on whether it can provide trustworthy project mapping, consent-based recipient identity, explainable policy choices, and reliable payment reconciliation at scale. Before a broader deployment, the project would need user research, improved evidence provenance, security and privacy review, backup/restore operations, multi-tenant access design, and a deliberately supported production payment model. These are future work, not current capabilities.
