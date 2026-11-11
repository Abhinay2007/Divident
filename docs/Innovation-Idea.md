# Innovation / Idea

## The idea

Dividend connects an installed software inventory to a reviewable funding proposal. A donor starts from the packages their system depends on, follows those Debian source packages to upstream projects, sees the evidence available for contributor attribution, and can route a donor-approved funding plan through PayPal Sandbox.

The idea is to make an open-source funding decision more structured and explainable. Dividend does not claim that software usage uniquely determines a project's value or that one allocation formula can settle every donor's priorities.

## The problem behind the idea

Software users often encounter open-source projects indirectly through binary packages, transitive dependencies, or distribution-maintained packaging. Preparing a funding decision then requires several separate tasks: identify the upstream project, understand what evidence supports the mapping, review contributor activity, decide how much to allocate, distinguish an intended allocation from money actually moved, and preserve an audit trail.

Manual donation pages and package inventories solve different parts of this process. Dividend explores a workflow that joins the inventory-to-upstream path with evidence review, deterministic funding rules, and explicit payment actions. This is a product hypothesis and technical design, not a verified comparison showing that all existing alternatives lack these capabilities.

## The combined workflow

The differentiation is the interaction of several implemented parts:

1. **Dependency discovery.** `dividend/scan.py` reads installed Debian package status and groups binary packages by source package. Existing upload and paste paths accept other inventories.
2. **Upstream mapping.** Structured UDD/GitHub facts, family/no-upstream overrides, URL normalization, and an allowlisted official-source verifier create explainable groupings. Unmapped sources remain excluded.
3. **Evidence-based attribution.** Maintainer eligibility is distinct from project mapping. An opted-in, active recipient and sufficiently supported server-validated evidence are required for a payable share; unsupported shares remain reserved.
4. **AI-assisted interpretation.** Modal/DeepSeek can summarize validated contributor evidence for the donor. It returns structured text and cited PR numbers; it has no authority over allocation or payment.
5. **Deterministic allocation.** A pure policy-driven function applies usage and need factors, caps, a minimum line, redistribution, and largest-remainder rounding. The resulting proposal is stored with its canonical hash.
6. **Donor-controlled execution.** PayPal order creation, payer approval, capture, and payout are distinct actions. The app uses Sandbox credentials and persists guarded state transitions and ledger events.

The innovation is not any one API call or formula; it is the attempt to connect software dependency context with a transparent, reviewable and payment-aware funding workflow.

## Evidence and identity are separate questions

Contribution evidence can indicate activity associated with a GitHub account. That alone does not establish that a person controls a particular recipient identity or is an eligible maintainer. Dividend keeps evidence collection/validation, GitHub account claims, recipient opt-in, market eligibility, and payment recipient selection as separate records and checks.

The repository's bundled curl snapshot uses a mapping file that explicitly labels the identity mapping synthetic and not a verified identity claim. A separate **SYNTHETIC SANDBOX DEMO** uses fictional contributor labels and `synthetic://` evidence. It exists to show how payment orchestration works with an isolated Sandbox destination; it must not be used as a claim about real-world eligibility.

## Why deterministic allocation matters

The money path is deliberately outside the model. For a fixed project input set, budget, and allocation policy, the allocation engine returns explainable integer-cent lines whose total matches the budget. It applies caps and minimums, redistributes eligible amounts, and splits lines into payable and reserved amounts. Proposal snapshots retain the calculation context and a hash so payment services can verify the plan again before acting.

The policy is inspectable in `data/allocation_policy.yaml`. Its displayed fee parameters are estimates under declared US/USD assumptions, not universal PayPal prices. Actual provider-reported fee data is recorded during reconciliation.

## Reserved funding as an explicit outcome

The system does not have to turn every project allocation into a maintainer payment. If a project lacks an eligible opted-in recipient or its contribution evidence is incomplete, some or all of its amount can remain reserved. That makes uncertainty visible in the proposal and prevents unsupported attribution from becoming a payment line.

The separate synthetic attribution policy is fixed and versioned for Sandbox demonstration only. It does not weaken real recipient or evidence checks.

## Donor control and financial accountability

PayPal Sandbox use is divided into:

- proposal review and explicit order creation;
- payer approval in PayPal;
- a separate donor-confirmed capture;
- a separate payout action after funding is confirmed;
- provider status reconciliation per payout item.

This structure makes intended funding, payer-approved orders, captured funds, and confirmed payout item statuses distinguishable. Request IDs, attempt counters, guarded transitions, provider-response checks, and ledger events support retries and investigation. It is a Sandbox workflow: the code does not claim to move real funds or prove that an unclaimed/pending payout reached a person.

## Practical novelty and comparisons

Dividend's design combines installed dependency discovery, upstream mapping, evidence review, policy-based allocation, reserve handling, and donor-approved PayPal orchestration. The repository does not contain a market study or benchmark comparing Dividend against donation platforms, package sponsorship systems, or fiscal hosts. Therefore, this document makes no claim that Dividend is the first product to address open-source funding or that alternatives do not offer overlapping capabilities.

## Current limitations

- The running session workflow does not wire the LLM upstream resolver helper into source resolution; unresolved packages remain unmapped.
- The live public GitHub evidence collector exists in the codebase but is not exposed as a user-facing collection route.
- Bundled contribution evidence uses a synthetic identity mapping. The separately labelled synthetic lane is fictional by design.
- GitHub maintainer claims require an operator-configured OAuth application, and normal payment requires correctly opted-in recipient records.
- AI review is optional, bounded, and evidence-dependent. It can be unavailable without affecting deterministic allocation.
- `render.yaml` currently selects a fake payment provider; a Render deployment from this manifest does not perform the PayPal Sandbox workflow.
- PayPal provider configuration is Sandbox-only. Production payouts and operational scaling are outside the current implementation.

## Future opportunities

Potential research and product work includes broader verified package-to-upstream coverage, a maintainers' consent workflow with clearer evidence provenance, explicit freshness/quality measures for snapshots, a donor-configurable policy that remains deterministic and reviewable, and a read-only ledger timeline for state/provider evidence. Each would need evaluation with real projects and maintainers; none should be presented as delivered today.
