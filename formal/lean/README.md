<!--
SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Buildlog: abstract discovery and shipped core invariants in Lean

This is a **conditional mechanization of the mathematical core**, not a verified
implementation of Python, the loader, or Soufflé. It uses Lean **4.35.0**, with
only Lean's bundled `Std` library. There are no external dependencies, project
`axiom` declarations, `sorry`s, `admit`s, or native-evaluation proof shortcuts.
The development accompanies the paper; implementation correspondence remains conditional.

## Build and audit

With elan and the pinned toolchain available:

```sh
cd formal/lean
lake build
```

A clean checkout needs no Mathlib download or olean cache. `.lake/` is ignored.
To recheck all files and display the axiom audit:

```sh
lake clean
lake build
```

`Buildlog/Axioms.lean` and `Buildlog/Core/Axioms.lean` contain `#print axioms`
for the headline discovery and core theorems,
and is imported by the default library target. The models use classical
finite sets and choice; they are proof objects, not a replacement executable
driver. A clean build checks the Lean source, including the example
counterexamples.

## Representation and semantic boundary

### Facts and strata

`Facts n := Fin n → Prop` represents subsets of a fixed indexed finite
universe. `Finite.lean` proves cardinality bounds, strict growth, and bounded
progress. Facts are sets: duplicates and order are intentionally absent.

`Positive` in `Semantics.lean` is an inflationary, monotone
immediate-consequence operator **within one stratum**. Its least fixed point
above a base is constructed by finite iteration, not postulated:
`Positive.fixed`, `Positive.least`, and `Positive.model_eq_of_agree` are proved.
`Stratified.stages` evaluates a finite list of strata, starting each with the
complete lower model.

Negation and aggregates may inspect that lower model through arbitrary
extensional tests. **No monotonicity in answers or lower-stratum inputs is
assumed.** Negation is generally antitone in its lower input. Monotonicity
applies only to positive recursive premises in the current stratum.

`Rules.lean` supplies a concrete finite ground-rule instantiation:

* a rule has a head, positive recursive premises, and an extensional test;
* the test records all answer and lower-fact dependencies;
* its extensionality contract includes positive, negative, and aggregate
  reads, without needing to implement each Soufflé aggregate separately;
* head/premise ranks record stratification;
* a direct positive matching request guard implies the local operator
  agreement used by the inertness proof.

The ground rule count is finite. Grounding, dependency extraction, term
construction, arithmetic/functor evaluation, and translation of Soufflé
syntax are **not** verified.

This is a stronger *representation* than the paper's (F): it supplies a
common finite ground base, rather than proving only that each finite answer
set has a finite model. For fixed finite correct-answer universes in
`thm:lfp`, (F) gives a common finite base by taking the union of models over
the finitely many correct-answer subsets; that reduction is not itself
formalized. For arbitrary snapshot-dependent β, the finite-base reduction is
also not formalized. Thus the Lean results apply directly to the finite
grounded abstraction, not yet to every term-constructing program satisfying
only the paper's (F).

### Inputs, layers, identifiers, and oracle answers

The composed program, author facts A, user facts U, and fixed environment are
parameters captured by the stratum operators/model function. No rules or
author/user inputs change within a pass. Manifest role classification,
origin tracking, layer visibility, permitted extensions of open relations,
and the term grammar (including sorted setting lists) are outside this
development. Ground terms are opaque elements of the finite universe.

There are two deliberately distinct answer representations:

* `Driver` uses finite **row sets** and a snapshot-dependent
  `beta : Fin n → Facts a → Facts a`. `n` indexes request keys
  `(kind, identifier)`; the unique argument tuple determined by the model is
  implicit in β's state-dependent call. `primary` identifies the primary
  rows answering a key. There is no fixed-answer (A) assumption for these
  fresh-driver proofs.
* `Stratified`/`Closure` use sets of **fixed correct answer bundles**.
  Membership of a request index means inclusion of its entire α answer.
  (A) and (I), total primary rows, and row ownership (RL) are requirements of
  interpreting those indices as actual driver answers. The extensional
  read-dependency contract expresses that a changed bundle affects only the
  result reads assigned to it. The row-flattening interpretation and
  α/β correspondence for the Python driver is not verified.
  `BundleProgram` now constructs fixed-bundle semantics from finite
  row-level ground rules and explicit row ownership; `model_flatten`
  proves agreement of their models.

`Stratified.model` contains **non-probe-result** facts. Equality here is the
paper's `≈`, not equality of full models including retained answer rows.
Requests are among these non-result facts, so model agreement preserves them.
An arbitrary deterministic renderer can turn the model and final row set
into an outcome; `Driver.outcome_deterministic` proves preservation of that
renderer. Actual canonical error sorting, graph sorting, exit statuses,
`--keep-going`, and filesystem writes are external contracts.

### Fresh rounds and checks

`Driver.pending`, `batch`, `produced`, and `step` model the paper's fresh
schedule. If any guarded request is pending, the batch contains precisely
the guarded pending requests; otherwise it contains all pending requests.
Every β invocation reads the same starting snapshot.

`badBefore` represents domain/tuple checks of **all requests**, before any
probe. `badAfter` represents the collected shape/declaration/primary checks
of the selected batch. They are pure functions; their concrete validators
and canonical violation reports are not verified.
`total_if_checked` is explicitly the successful branch of the primary-row
check, **not** an assumption that every unchecked probe is total.

A round either halts (success or a check failure), or strictly increases
the number of answered keys. The bound is `k ≤ n` for the first stopping
round indexed from zero, hence at most **`n + 1` model evaluations**.
Distinct key count `n` is no larger than the paper's sum of request-tuple
universe sizes; the tuple-to-key cardinality reduction is external.
Out-of-universe malformed requests are abstracted by `badBefore`, not
represented as extra `Fin n` indices.

(P0) is represented by total Lean functions returning finite sets, not by
a verified timeout/termination argument for Python or fingerprint calls.
The fresh mathematical oracle is β, not an unchecked persistent cache;
see the cache discrepancy below.

### Inertness and least closure

The semantic guardedness contract is **local operator agreement**, not
presumed final-model inertness. The proof compares positive iterates of
one chosen model, and then inducts through strata. Both absence-before
and absence-after orientations are proved. This includes same-stratum
positive request guards; no acyclic-request assumption is added.

`GroundStratum.guarded_operator` proves that direct syntactic request
guards discharge the contract. The loader's **auxiliary** guard
certification is not mechanized: arbitrary interpretations can contain
an auxiliary fact without its request, so its soundness requires an
iterate/derivation invariant or normalization to direct guards. This
additional work is not silently assumed to have been proved.

`Closure.lean` proves existence of closed guarded sets, equality of their
non-result models, the least closed set, and uniqueness of that least
set. Existence can use the full finite guarded universe; leastness is
obtained by inertness and pruning, **not** Knaster–Tarski on requests.
`Examples.lean` checks that withdrawing requests are not monotone in
answers, and that mutually negated unguarded requests have no least
closed answer set.

`round_model` proves the phase lemma's semantic conclusion once confirmed
answers are correct and every unconfirmed request is absent. Phase 3 adds
the FC/FL confirmation induction and a row-level extension for arbitrary
wrong inactive seeds, described next.

### Cache confirmation and completed configure agreement

`CacheConfirmation.active_answers_correct` inducts over strict prerequisite
ranks. Active prerequisites are confirmed; oracle calls on their correct
answers return α. A cache hit requires an equal key and valid watches, and
FC equates its stored payload to that call. Payloads can be whole row bundles.
Inactive tentative payloads need not be correct. These are explicit FC/FL
premises, not domain-specific axioms or assumed confirmation correctness.

`BundleProgram` supplies the RL/I row-to-key boundary and derives its
fixed-bundle `Stratified` semantics from the existing guarded ground rules.
`CachePhase.reduced` combines confirmation correctness with row-level
inertness: even arbitrary wrong tentative rows are inert when their owners'
requests are absent. It derives guarded closure and the phase model M_F.

`CachePhaseRun.follows_schedule` then proves that a successful fresh or seeded
run's unguarded phase answers follow the deterministic package schedule from
empty F. `package_termination` bounds its strict growth by the key universe;
`completed_phase_run` identifies any terminal phase with its canonical outcome.
`CompletedConfigure.canonical` also handles completed replay by induction on
record provenance: its package keys come from a previous certified run, while
current payloads are validated anew. Fallback retains only the retry's outcome.
`cached_fresh_agree` proves equality for any renderer of non-result facts.
`CacheExamples.lean` instantiates fresh, hit and replay certificates.
Certified (`active`) answers need not have requests in the current model:
completed replay certificates admit withdrawn guarded and unguarded answers.
Their prerequisite closure and correctness remain certificate premises.
The theorem does not equate retained probe-result rows or final input files;
a guarded-seeded run can omit a transient answer retained by a fresh run.

This is a **completed-phase certificate** mechanization of `cor:seeding`,
not an operational verification of the seed loop. Extracting these certificates
from Python, establishing confirmation prerequisite closure from real FL
derivations, CK, actual fingerprint/watch checks, stale-seed/probing-round
termination, the withdrawn-list protocol and replay-record serialization remain external. In particular,
the fallback constructor does not assert equivalence of an abandoned failure.
There is no result claiming cached success equivalence without CK. Truly fresh
certificates use only the oracle alternative; an unseeded-but-cache-using run
requires FC just like a seeded run.

### Prepared union and dependency isolation

`Preparation.lean` models a fixed finite graph of declared roots, metadata
includes and rule pointers, determined without demand or probe answers.
It proves termination and the least reachable closure. Completeness of
provider enumeration, strict executable-union validation, filesystem
observation certificates and Python traversal are external obligations.
Composition consumes that closure once; environmental rounds cannot add code.

For an all-guarded prepared program, `Stratified.fixed_union_closure`
specializes the existing least-closure theorem to **empty unguarded answers**.
`Driver.all_guarded_batch` makes the guarded-first schedule simply all pending
requests. `CachePhase.fixed_union_agreement` discharges the empty-F boundary
from its all-guarded premise and gives one-phase agreement with the canonical
model, including arbitrary inactive seeds under the existing FC/FL certificate
premises. No package-phase or composition-restart certificate is needed.
These results do not prove that the Python loader extracts sound guards.
The generic driver still permits explicitly justified unguarded plugin kinds;
the all-guarded corollaries apply only when their premise holds.

Available definitions, binding and use are distinct. Bound declarations can
affect conditions; only effect payloads are use-guarded. The module compiler's
dependency classification and guard insertion are tested, not verified here.
The existing dependency-slice and guarded-rule results supply the conditional
semantic boundaries; they do not prove that every dormant module is invisible.

The following restart models are retained as **historical abstractions**, not
as the current package-composition architecture:

`Restarts` takes deterministic normalized directory demand D as a
parameter. Roots grow by union; at termination the consistency check
either yields `L = L₀ ∪ D(L)` or fails. Under monotonicity (K), the check
passes and the roots are least. Arbitrary grouping/order is covered
provided steps only add pending directories, preserve roots, make
progress when nonterminal, and stop when none remain.

`Offers.lean` separately represents `(directory, reason)` tracking.
It proves finite progress and that a restart with unchanged roots cannot
stutter twice. The actual resolver closure C and
`D(L) = (H(L) ∩ L) ∪ (H(L) \ C(L))` are not implemented.
The same-demand premise for cached/fresh passes can now be obtained by applying
`cached_fresh_agree` to certified completed passes and the directory-demand
renderer. Extraction of those certificates from the implementation is external.

`Protected` in `Dependency.lean` is a dependency-closure certificate:
all positive and lower dependencies of protected heads are protected,
and no such rule reads answers. Its soundness for ground-rule
immediate-consequence operators and stratified model evaluation is proved.
This gives answer independence of decision facts. Verifying that the
loader's graph/expanded AST extraction supplies this certificate remains
external.

For errors, the original `errors_do_not_steer_discovery` and
`final_errors_agree` remain reusable conditional reductions.
`CompletionGate.lean` now discharges their W4/gate obligations for finite
ground rules. `CompletionProgram.Isolation` is a dependency-closed slice
excluding completion and changed rule heads, while protecting requests,
domains and model-based discovery checks. Unlike the older
`IndependentSlice` interface, it permits answer-dependent protected facts
and compares the two programs on the **same** answer input.

`Isolation.models_agree`, `discovery_agrees` and `halt_agrees` derive protected
model, trace and stopping-check agreement. `Definition.no_pending` derives
absence of generated pending facts from completed requests and the local
pending-rule premises. `complete_holds` then fires the complete clause.
`GroundProgram.lower_rank_stable` transfers this fact from the final model
to every changed rule's **evaluated lower model**, using the strict gate/head
rank ordering. `Definition.final_models_agree` now proves whole non-result
model equality, and `Isolation.prop_errors` combines the trace, stopping and
final-model conclusions. Error/target/action projections follow immediately.
AST dependency extraction, lowering of real generated clauses and the full
loader's W1–W6 validation remain external; no final operator-equality premise
is hidden in these new ground-rule theorems.

## Paper-to-Lean map

Labels below are the literal labels in the two `.tex` files, before
cross-document `M-`/`S-` prefixes. All named theorems have checked proofs.
“External” does not mean an admitted Lean theorem: it means no theorem
claiming that implementation-specific result is present.

| Paper result / label | Lean theorem(s), in namespace `Buildlog` | File and scope |
|---|---|---|
| Unique model, `prop:model` | `Positive.fixed`, `Positive.least`, `Positive.model_eq_of_agree` | `Semantics.lean`; constructed least stratum models, not Soufflé correctness |
| Fresh termination, `thm:termination` | `Driver.fresh_termination`, `Driver.first_stopping_round` | `Driver.lean`; fresh finite abstraction, including check failure, `n+1` evaluations |
| Fresh determinism, `thm:determinism` | `Driver.fresh_determinism`, `Driver.probe_order_independent`, `Driver.stopping_round_deterministic`, `Driver.outcome_deterministic` | `Driver.lean`, `Consequences.lean`; functional snapshots, set ingestion, deterministic rendering |
| Least closed guarded answers, `thm:lfp` | `Stratified.least_closed_guarded_answers`, `Stratified.least_unique`, `Stratified.closed_models` | `Closure.lean`; fixed correct bundles, finite semantics, local guardedness |
| Finite rounds, `lem:finite` | `Stratified.finite_rounds`, `Positive.fixed` | `Semantics.lean`; stronger common finite-base representation, not general constructed-term finiteness |
| Inert answers, `lem:guard` | `Stratified.inert_answers`, `ground_inert_answers`, `GroundStratum.guarded_operator` | `Semantics.lean`, `Rules.lean`; both orientations; direct guard instantiation proved |
| Bounded preparation, `prop:preparation` | `Preparation.terminates`, `Preparation.closed`, `Preparation.least` | `Preparation.lean`; fixed finite provider graph; enumeration/certificates external |
| All-guarded fixed union | `Stratified.fixed_union_closure`, `Driver.all_guarded_batch`, `CachePhase.fixed_union_agreement` | `Preparation.lean`; empty-F specialization and completed confirmation certificates |
| Explicit target requests are answer-free, `prop:options` | `Protected.explicit_target_requests_answer_free` | `Dependency.lean`; ground dependency certificate soundness proved; loader extraction external |
| Errors do not steer discovery, `prop:errors` | `CompletionProgram.Isolation.models_agree`, `discovery_agrees`, `halt_agrees`, `prop_errors`; `CompletionProgram.Definition.no_pending`, `complete_holds`, `final_models_agree` | `CompletionGate.lean`, `Provenance.lean`; W4 and gate obligations discharged for certified ground rules, including final error/attribution equality; loader/AST certificate extraction external. Original reductions remain in `Consequences.lean`. |
| What a round's model is, `lem:phase` | `Stratified.round_model`, `CacheConfirmation.active_answers_correct`, `CachePhase.reduced` | `Closure.lean`, `CacheConfirmation.lean`, `CacheSeeding.lean`; FC/FL induction and arbitrary inactive row-seed inertness proved; completed-phase certificate extraction external |
| Well-formed composition, `def:wellformed` | `GroundProgram`, `Protected`, `Driver` | `Rules.lean`, `Dependency.lean`, `Driver.lean`; semantic interfaces, not complete W1–W6 syntactic validation |
| Fixed answers for shipped kinds, `lem:fixed` | — | External: shipped inventory/prerequisite code not verified |
| Shipped rules have finite rounds, `lem:shipped-finite` | — | External: term-construction/column audit not verified |
| What an export states, `lem:export-exporter` | `Core.ExportInput.export_property_coverage`, `error_free_lossless_properties_spelled`, `foreign_rules_lost_with_warning` | `Core/Export.lean`; **partial**: existing relocated property rows, active component-capable file formats; nonexclusive coverage. Requirements, relocation and exporter completeness remain open. |
| Cached/fresh agreement, `cor:seeding` | `CachePhaseRun.follows_schedule`, `BundleProgram.package_termination`, `completed_phase_run`; `CompletedConfigure.canonical`, `cached_fresh_agree` | `Bundles.lean`, `CacheSeeding.lean`; completed-phase certificates, FC/FL confirmation, deterministic package schedule, replay provenance and retry-only fallback; operational extraction/termination and CK external |
| Toolchain uniqueness, `lem:toolchain`, §4 core invariant | `Core.ToolchainInput.toolchain_uniqueness`, `multiple_unpinned_candidates_error`, `conflicting_pins_error`, `noncandidate_pin_error`, `no_silent_choice`, `required_selection_outcome` | `Core/Toolchain.lean`; shipped selection/error clauses, including required-configuration no-selection errors; see explicit configuration premises below. |
| Tool configurations, `prop:exec`, §4 core invariant | `Core.ToolInput.tool_configuration_invariant`, `typed_reference_unique_host_instance`, `tool_configuration_functional`, `built_tools_functional`; `Core.ExecutionContext.tool_spec`, `tool_unique`, `tool_host`, `tool_idempotent` | `Core/Tools.lean`, `Core/FunctionalTools.lean`, `Core/Declarations.lean`; relational form plus **functional form** on the checked declaration model, with a present unique host and no transcribed settings/exec error. No exec-existence or name-existence premise in the functional form. |
| Execution totality supporting `prop:exec` | `Core.DeclarationInput.base_exec_total`, `base_exec_exists_unique`; `Core.ExecutionContext.known_exec_total` | `Core/Declarations.lean`; every shipped-derived base configuration, then every base/exec-generated configuration. |
| Error-free export exclusivity supporting `lem:export-exporter` | `Core.ExportInput.error_free_row_exclusive`, `row_spelled_projects`, `rejected_row_lost_with_warning` | `Core/Export.lean`; original property key/value and producing file retained; component-capable active file formats, existing relocated rows. Not full exporter completeness. |
| Corrected E1 implication supporting `prop:export` | `Core.ExportInput.error_free_E1_row_spelled`, `no_loss_alone_insufficient` | `Core/Export.lean`; error-free export plus local no-loss implies this row is spelled; finite counterexample proves no-loss alone insufficient. |
| Downstream export use, `prop:export`, `lem:export-downstream` | `Core.export_usage_roundtrip` | `Core/Export.lean`; **conditional abstract propagation theorem**, assuming exact relocated own-row correspondence and matched requirement edges. Concrete exporter/importer inversion and identification of the full shipped propagation slice remain open. |
| Layers/programs/inputs and Terms definitions (unlabelled) | Ground program and opaque finite facts | Grounded representation only; no manifest parser or term grammar formalization |

## Assumptions versus axioms

There are **no domain-specific Lean axioms**. Structures and theorem
arguments carry the semantic assumptions; they do not disappear merely
because `#print axioms` reports only logical axioms.

| Paper condition | Lean interpretation / unverified obligation |
|---|---|
| Fixed P, A, U; W1 | Fixed staged operators; concrete rule ranks and positive monotonicity. Soufflé parsing, composition and engine agreement external. |
| F | Common finite model/answer bases; least stratum computation proved. General and shipped constructed-term finiteness external. |
| P0 | Returning total model/oracle/check functions. Python/fingerprint return obligation external. |
| P1 | `Driver.total_if_checked`; checked successful branch. Concrete validator correctness external. |
| P2, W2 | Fixed finite key indices, membership/check abstraction. Universe derivation and tuple/key bound external. |
| P3 | Functional `beta` reading the starting snapshot. Probe-order set union proved. Real probe functionality external. |
| A, I, RL | Fixed correct bundles and local reads; `BundleProgram` constructs/proves the finite row-flattening boundary under explicit ownership and request-guard correspondence. Real tuple encodings and Python α/β interpretation external. |
| W3 | Local guarded operator agreement; direct ground-rule guards discharge it. Auxiliary guard certification external. |
| CK | Correct-bundle evaluation is available for all subsets; concrete universal passing-check argument external. Closure theorem has no hidden fresh-only restriction. |
| W4 | `CompletionProgram.Isolation` dependency closure excludes the completion fact and changed heads; shared Python row checks. Same-input slice, trace/check agreement and final gate discharge proved. Actual loader graph/AST certificate extraction external. |
| W5 | `Protected` certificate soundness proved; loader certificate extraction external. |
| W6 | One model-determined call per key; tuple uniqueness validation external. |
| FC, FL | Explicit equal-key/valid-watch hit soundness and active prerequisite closure/strict rank premises; confirmation correctness derived, then completed-phase cache equivalence proved. Real key/watch soundness and FL certificate extraction external. |
| Finite packages, K | Finite root indices; deterministic D; optional monotonicity explicitly required for leastness/grouping independence. |
| E1–E5 | Export property coverage does not require them. Abstract usage reconstruction assumes the own-row and requirement correspondences they are intended to establish, plus identification of the isolated/neutral propagation slice; it does not derive those correspondences from the paper's E1–E5 or verify concrete importers. |
| Checked execution context | A present unique host and absence of `DeclarationInput.SettingsError` / `ConfigInput.Diagnostic`. Base/suffix provenance and input domains are now proved from the declaration rules, not assumed complete. Primitive policy/platform/declaration facts, symbol ordinal injection, and Soufflé rule agreement remain the semantic boundary. |

### Actual axiom audit

For **each** of the following audited theorems, the exact output is
`[propext, Classical.choice, Quot.sound]`:

* `Facts.finite_progress`, `bounded_progress`
* `Positive.fixed`, `Positive.least`
* `Stratified.finite_rounds`, `Stratified.inert_answers`, `ground_inert_answers`
* `Stratified.closed_exists`, `Stratified.closed_models`
* `Stratified.least_closed_guarded_answers`, `Stratified.least_unique`, `Stratified.round_model`
* `Driver.fresh_termination`, `Driver.first_stopping_round`, `Driver.fresh_determinism`
* `Driver.probe_order_independent`, `Driver.stopping_round_deterministic`, `Driver.outcome_deterministic`
* `Driver.guarded_first`
* `Restarts.restarts_terminate`, `Restarts.least_restart_roots`, `Restarts.grouping_terminates`
* `stutter_cannot_repeat`, `offer_loop_terminates`
* `Protected.explicit_target_requests_answer_free`
* `IndependentSlice.models_agree`
* `Driver.errors_do_not_steer_discovery`, `Stratified.final_errors_agree`
* `BundleProgram.model_flatten`, `package_termination`, `completed_phase_run`
* `CachePhase.active_rows_correct`, `CachePhase.reduced`, `CachePhaseRun.follows_schedule`
* `CompletedConfigure.canonical`, `cached_fresh_agree`
* `Examples.cache_hit_matches_cache_free`, `replay_matches_cache_free`
* `GroundProgram.model_source`, `lower_rank_stable`
* `CompletionProgram.Isolation.models_agree`, `discovery_agrees`, `halt_agrees`, `prop_errors`
* `CompletionProgram.Definition.no_pending`, `complete_holds`, `final_models_agree`
* `Examples.gate_completion_derived`, `gate_final_errors_agree`, `gate_discovery_agrees`

The other audited outputs are:

| Theorem | Exact axioms |
|---|---|
| `Restarts.restart_determinism` | none |
| `Restarts.grouping_independent` | `propext`, `Quot.sound` |
| `GroundStratum.guarded_operator` | `propext`, `Quot.sound` |
| `Examples.request_operator_not_monotone` | `propext` |
| `Examples.unguarded_has_no_least_closed` | `propext` |
| `CacheConfirmation.active_answers_correct` | none |
| `Examples.stale_hit_violates_FC` | `propext`, `Quot.sound` |

These are Lean's standard logical axioms; `Quot.sound` here is inherited
through library/arithmetic proof infrastructure. No result uses `sorryAx`.

### Core and export axiom audit

`Buildlog/Core/Axioms.lean` audits all the core headline results. The following
groups give the exact output for each member (namespace `Buildlog.Core`).

| Namespace / theorem names | Exact axioms |
|---|---|
| `ConfigInput.minimum_unique`, `exec_targets_host`, `exec_targets_the_host`, `exec_config_functional`, `exec_config_idempotent` | none |
| `ConfigInput.exec_settings_functional`, `exec_settings_preserves_keys` | `propext` |
| `ToolchainInput.selection_exact`, `selection_has_capabilities`, `multiple_unpinned_candidates_error`, `noncandidate_pin_error`, `missing_selection_errors_every_action`, `no_selection_errors_every_action` | none |
| `ToolchainInput.conflicting_pins_error`, `toolchain_uniqueness`, `no_silent_choice`, `required_selection_outcome` | `propext`, `Classical.choice`, `Quot.sound` |
| `ToolInput.direct_dependencies_exact`, `direct_tool_need_exact`, `needed_minimal`, `dependency_minimal`, `no_roots_no_needed`, `tool_configuration_invariant`, `typed_reference_unique_host_instance`, `built_toolchain_tools_needed`, `scalar_path_needs_host`, `list_path_needs_host`, `built_path_uses_host`, `missing_tool_artifact_error` | none |
| `ExportInput.export_property_coverage`, `error_free_lossless_properties_spelled` | `propext`, `Classical.choice`, `Quot.sound` |
| `ExportInput.overlapping_tables_error`, `foreign_rules_lost_with_warning`; `export_usage_roundtrip` | none |

Phase-2b additions, each audited in `Core/Axioms.lean`:

| Namespace / theorem names | Exact axioms |
|---|---|
| `ConfigInput.inherit_override_preserves_value`; `DeclarationInput.bad_reports_error`, `base_settings_declared`, `error_free_reserved_absent`, `rank_declared`; `ExportInput.row_spelled_projects`, `rejected_row_lost_with_warning`, `no_loss_alone_insufficient` | none |
| `DeclarationInput.key_priority_unique` | `propext`, `Quot.sound` |
| `ConfigInput.minimum_exists`, `minimum_idempotent`, `exec_config_functional_of_host` | `propext`, `Classical.choice`, `Quot.sound` |
| `DeclarationInput.exec_value_exists`, `base_entries`, `exec_settings_total`, `base_exec_total`, `base_exec_exists_unique` | `propext`, `Classical.choice`, `Quot.sound` |
| `ExecutionContext.known_exec_total`, `tool_spec`, `tool_unique`, `tool_host`, `tool_idempotent` | `propext`, `Classical.choice`, `Quot.sound` |
| `ToolInput.tool_configuration_functional`, `built_tools_functional` | `propext`, `Classical.choice`, `Quot.sound` |
| `ExportInput.error_free_row_exclusive`, `error_free_E1_row_spelled` | `propext`, `Classical.choice`, `Quot.sound` |

## Discrepancies and proof obligations exposed

The following distinguish actual statement/source problems from limitations
of this bounded mechanization. No paper `.tex` file has been changed.

### 1. “Fresh” Python discovery still reads persistent probe cache

**Historical finding; independently addressed in main, commit `835dd92`,
consumed by merge `167684e`.** The new `configure --fresh` / `fresh=True`
path bypasses cache reuse and seeds. Merely setting `seed=False` remains
different from genuinely fresh mode. The discussion below records the
original counterexample, not a claim that the new fresh path is broken.

**Operational interpretation gap in `thm:determinism`.** The mathematical
fresh loop calls β starting from empty answers. But
`src/buildlog/discovery.py` loads the probe cache regardless of `seed`, starts
without seeds at lines 403–404 when `seed=False`, and still reuses cache rows
at lines 587–595. Thus empty seeds do not mean cache-free oracle calls.
The theorem's hypotheses list P3, not FC.

Counterexample to interpreting the theorem as a guarantee for every
unseeded Python run: one guarded constant request, a probe returning “yes”
in an old environment and “no” in the current one, a constant fingerprint,
and no watches. The probe and fingerprint satisfy P3 and return; the
singleton domain and model are finite. One build directory has a validly
shaped old cached “yes”; another has no cache and calls the probe for “no”.
Same current A/U/environment, options and β, different derived usage facts.
FC fails, which the fresh theorem does not require.

**Suggested correction:** define mathematical “fresh” explicitly as
cache-free β evaluation, and separately require FC for claims about the
implementation's unseeded-but-cache-using path; alternatively bypass
persistent cache in a genuinely fresh mode. The Lean theorem proves the
cache-free mathematical schedule, not the stronger operational reading.

### 2. Export classification is not proved exclusive

**Historical overlap-check gap; independently addressed in main, commit
`835dd92`, consumed by merge `167684e`.** Both overlaps now derive errors.
Spelling/loss facts still coexist in erroneous models: the corrected
nonexclusive coverage statement is proved in `Core/Export.lean`. Thus the
old unconditional “exactly one” wording still requires qualification by
successful/error-free processing, not just the presence of the new checks.

**Statement issue in `lem:export-exporter`: “exactly one”.**
`src/buildlog/layers/core/L3_install.dl:532–536` derives loss/warning from
`export_unspelled`; lines 563–568 independently derive spelling from
`export_spelling`. Both convention relations are open
(`src/buildlog/layers/core/manifest.toml:515–517`).

A format extension may state both
`export_spelling(f,k,x)` and `export_unspelled(f,k,why)`.
For an active component-capable format and an exported non-value fact
with key k, the displayed rules derive **both** spelling and loss/warning.
The derivation was a source-level counterexample in Phase 1. At that
revision there was no rejection for this overlapping table. The new
rejection does not retract either derivation. Value-spelling errors can
also coexist with an unspelled declaration.

**Suggested correction:** replace “exactly one” with a coverage statement
“spelled, or loss/warning, or an export error”; or add a checked, explicit
well-formedness condition on spelling tables and specify that errors are
attributed to the particular fact. Do not infer exclusive classification
from the current rule inspection alone.

### 3. No loss does not by itself eliminate the export-error alternative

**Phase 2b:** the gap is now a checked finite counterexample
`ExportInput.no_loss_alone_insufficient`. The corrected implication
`error_free_E1_row_spelled` requires error-free export as well as E1's
local no-loss premise, and retains the original row's key/value.

**Missing explicit premise in the displayed proof of
`prop:export`/`lem:export-downstream`.** The proof says that the export lemma
plus E1 implies every own fact is spelled. However E1 excludes loss facts,
not the export lemma's **error** alternative. A missing spelling or an
unspellable value can derive an error without a loss fact.

Actual successful exported files may supply the missing premise implicitly:
configure errors can prevent their production. But “an export” and the
per-component/configuration success condition must then be defined
explicitly; no-loss alone is not the logical implication used in the proof.

**Suggested correction:** require an error-free, successfully emitted export
for the component/configuration and its requirements, or strengthen E1
to exclude both loss and export-affecting errors. This is a proof-premise
clarification, not a claim that a successfully emitted shipped export
has been demonstrated wrong.

### 4. Phase-lemma hypotheses need their ambient closure context

**Statement clarity issue, not a counterexample.** `lem:phase` explicitly
lists RL/A/FC/FL/I but invokes M_F and `thm:lfp`; stratification, F,
guardedness and the all-correct-subsets universe/check conditions must
still come from the surrounding context. An isolated reading of that
lemma does not provide them.

**Suggested clarification:** begin “Under the hypotheses of
Theorem `thm:lfp`, additionally assume FC and FL …”.
The Lean phase theorem explicitly takes the stratified guarded model and
correct bundle interpretation. Phase 3 derives confirmation correctness
from FC/FL and extends the phase proof to arbitrary wrong inactive row seeds;
the ambient grounded/guarded closure context remains explicit.

### 5. Completion and auxiliary guards require more than a model equality slogan

**Completion/W4 discharged in Phase 3 for finite ground rules; auxiliary
guards remain open.** These were mechanization obligations, not demonstrated
paper errors. Truth of
`complete` in the final model alone is not a proof that all intermediate
immediate-consequence operators agree after gate removal. One needs a
stratum partition placing completion below changed propagation rules
(or an equivalent guarded least-iteration proof), and completion true in
their evaluated lower inputs. `final_errors_agree` makes this obligation
explicit. `CompletionProgram.gate_below` now records that strict W1 ordering.
`Definition.no_pending` and `complete_holds` prove completion from generated
rule premises, and `GroundProgram.lower_rank_stable` places it in each evaluated
lower input. `Definition.final_models_agree` discharges the reduction without
assuming operator equality. `Isolation.prop_errors` additionally derives
discovery/check equality from the W4 dependency certificate. The remaining
syntactic boundary is extracting these local rule/dependency certificates
from the loader's expanded AST, not a missing semantic gate argument.

Likewise, auxiliary guard entailment is an invariant of derivable
interpretations, not arbitrary sets containing unrelated auxiliary
facts. The paper's derivation argument is the right kind of argument;
this development proves direct guards and leaves the loader's
auxiliary-certification induction open.

### 6. Finite ground bases are not silently substituted for the shipped F audit

**Mechanization limitation.** The Lean finite-base assumptions are stronger
than the paper's per-input F formulation, as explained above. No claim
is made that finite-index types mechanize `lem:shipped-finite` or the
unbounded-term audit. A future phase must verify the common-base reduction
or work with arbitrary ground terms and finiteness certificates.

There is no discovered counterexample here to the paper's guarded leastness
or either inertness orientation. The request operator is deliberately
allowed to withdraw requests, and the unguarded schedule is not claimed to
be a least fixed point.

### 7. A total `tool(c)` needs more than existence of a model

**Phase-2b discharge:** on the shipped-derived declaration model, a present
unique host and no transcribed settings/exec error imply exactly one
destination for every base configuration (`base_exec_exists_unique`).
`ExecutionContext.tool` extends this to every known base/exec-generated
configuration. No name-totality or destination-existence hypothesis is
needed. The historical statement concern remains for “model exists” alone;
the extra error-free/host hypotheses must still be stated.

**Phase-2 premise clarification for `prop:exec`.** The core rule is relational:
`needed(g,e) :- tool_dep(_,c,g), exec_config(c,e)`. A completed model need not
contain an execution destination for every configuration. For example,
conflicting execution policies can remove all `exec_value` rows for a key;
the aggregate/recursive `exec_settings` rule then cannot construct its
mapped settings. Missing policies or host rows can likewise prevent an
execution destination. Error reporting does not manufacture one.

The proved forward implication therefore explicitly starts with a
**resolved typed reference and an `exec_config(c,e)` derivation**. Its
destination is host-configured, even without an error-free premise.
Uniqueness additionally uses the actual configuration/name diagnostics and
existence of names. Identifying the host with the one build machine requires
a present, unique `host_platform` row. These are not silently inferred from
“the model exists”.

**Suggested precise statement:** every resolved typed reference from an
action owned in configuration c induces a tool dependency; for every
derived `exec_config(c,e)`, the referenced tool is needed in e and e's
platform is a host platform. On named, configuration-error-free inputs this
destination is unique when it exists. Use the functional notation `tool(c)`
only after separately establishing existence for the relevant
configurations. Phase 2 did not prove that totality obligation; Phase 2b
now proves it with the hypotheses above.

### 8. `"inherit"` as a domain value can silently change inherited settings

**Resolved independently in main, commit `a5685e4`.** The fixed-override
clause now requires `f != "inherit"`, and the new
`error("setting value is reserved", ...)` rejects `"inherit"` in a domain.
The Lean constructor retains the new guard and `SettingsError.reservedValue`
models the new check. `error_free_reserved_absent` proves that these inputs
are outside the checked execution context; `inherit_override_preserves_value`
proves that every derived value under an inheritance override is preserved.
Totality, uniqueness and functional tool theorems still compile unchanged
in statement. The runtime fixture now expects the reserved-value error and
the unchanged `"off"` value. Both new proofs are axiom-free.

The following records the **historical** source policy-interpretation bug
found during Phase 2b, not a missing execution destination. Before the fix,
the fourth `exec_value` clause accepted
`exec_setting(k,f)` whenever `setting_value(k,f)`, without excluding
`f="inherit"`. If `"inherit"` is in the domain, an inheritance override
derives both `exec_value(k,v,v)` and `exec_value(k,v,"inherit")`. The ordinal
minimum can choose the latter. Both outcomes are idempotent, so none of
the settings/exec checks detects the unintended change.

Small source-level reproduction against the shipped types/L0/L1 rules:

```souffle
platform("target", "target"). platform("host", "host").
host_platform("host"). config_decl("cfg", "target").
setting_key("k", 0).
setting_value("k", "off"). setting_value("k", "inherit").
setting_default("k", "off"). setting_exec_inherit("k").
exec_setting("k", "inherit").
```

On Soufflé 2.5 the fixture derives:

```text
exec_value(k, off, off)
exec_value(k, off, inherit)
exec_config([target, [[k, off], nil]], [host, [[k, inherit], nil]])
```

Its `error` relation was empty. `tests/test_lean_exec_totality.py` originally
reproduced this behavior using the then-current source, with unrelated request gates
empty. The result depends on the ordinal order; the demonstrated order
selects `"inherit"` instead of preserving `"off"`.

**Correction consumed from main:** both the fixed-override guard and the
reserved-domain check were added in `a5685e4`. No `.dl` files were edited
in this mechanization update.

This does not refute the proved totality, uniqueness, host-platform or
needed-closure results. The Lean function is the **actual ordinal-minimum
core destination**, not a silently corrected interpretation of inheritance.
Preservation under an inheritance override is now proved for the corrected
source, rather than assumed.

## Shipped core transcription: scope and manual correspondence

Phase 2 uses inductive **least derivations**, not an assumed functional
selection relation. Primitive/open relations and completed boundary
contributions are theorem parameters. Positive recursion is retained:
execution-image reflection, alias closure, and mutual `Needed`/`Dependency`.
`needed_minimal` and `dependency_minimal` prove the least-model eliminators
for that mutual slice; `no_roots_no_needed` excludes bootstrap without
ordinary roots **and** typed references. This is not a mechanization of
every rule in the complete program.

Symbols are abstract `Nat` ranks for equality/total diagnostic ordering on
the shipped snapshot. Soufflé `ord` is represented separately by an
injective `ordinal` function: it is not confused with the diagnostic order.
Settings and configurations preserve the record/list structure. Action
ranks and list indices are `Int`, covering the signed `number` domain;
the proofs do not depend on arithmetic overflow or machine width.

The following is the manually audited source-to-definition correspondence.
Names in the Lean column are under `Buildlog.Core`, with input namespaces
`ConfigInput`, `ToolchainInput`, `ToolInput`, and `ExportInput` as applicable.
Each listed constructor retains the clause's premises, including negative
guards. Diagnostic strings and concatenated display text are erased;
toolchain/no-toolchain diagnostics retain structured attribution.

| Shipped source / relation or rule | Lean transcription |
|---|---|
| `L1_targets.dl`: `exec_overridden`, three `exec_bad` clauses | `Overridden.stated`; `Bad.mixed`, `.defaults`, `.overrides` |
| Four `exec_value` clauses | `Value.inheritDefault`, `.fixedDefault`, `.inheritOverride`, `.fixedOverride` |
| `exec_settings(nil,nil)` and recursive ordinal-minimum clause | `ExecSettings.nil`, `.cons`; `Minimum` includes a row plus ordinal minimality, not presumed raw-policy functionality |
| Both `exec_config` clauses | `Exec.base`, `.reflected`; host destination and image reflexivity proved |
| `config(e) :- exec_config(_,e)`; `config_platform` | `Known.exec`, `Platform.config`; other configuration contributions frozen as `baseConfig`/`Known.base` |
| Shared-name, nonfunctional-exec, nonidempotent-exec errors | `Diagnostic.sharedName`, `.notFunctional`, `.notIdempotent` |
| `L2_toolchains.dl`: built exclusion, candidate, pinned, both selection clauses | `Built.tool`, `Candidate.available`, `Pinned.pin`, `Select.unpinned`, `.pinned` |
| `tc_used`, `tc_for`, `has_tc` | `Used.required`, `For.selected`, `Has.selected`, using transcribed execution/config-platform relations |
| Ambiguity, noncandidate pin, both pin-conflict diagnostics | `Error.ambiguous`, `.badPin`, `.doublePin`, `.authorDoublePin`; both author-decision branches retained |
| Required/no-selection `error_at("no toolchain",...)` | `NoToolchain.target`; joins configuration name and each `target_action` owner |
| `L1_targets.dl`: scope/project-name/alias candidates | `Scoped.scope`, `ScopeName.scope`, `ProjectName.target`; `NameCandidate.target`, `.alias` |
| Scalar/list `tool_ref`; four `tool_resolves` clauses; `tool_resolved` | `Ref.scalar`, `.item`; `Resolves.scopedLocal`, `.projectLocal`, `.scopedFallback`, `.projectFallback`; `Resolved.target`; `UniqueCandidate` models one distinct candidate tuple, not one derivation |
| `tool_dep` from references (L1) and built toolchains (L2) | `Dependency.reference`, `.builtToolchain`; latter retains owner-needed, selected-toolchain and role premises |
| `needed(g,e) :- tool_dep(_,c,g), exec_config(c,e)` | `Needed.tool`; all other `needed` contributions abstracted as `otherNeeded` / `Needed.other` |
| Scalar/list typed path multi-head expansions (L1) | `ScalarPath.expand`, `ListPath.expand`; shared derivations for argv/part and implicit-input heads, retaining prefix/path operands without verifying string rendering |
| Built-role path expansion (L2) | `BuiltPath.expand`; host artifact selection, not unconditional dependency inference |
| `has_run_artifact`; tool-dependency missing-artifact diagnostic | `HasArtifact.artifact`, `Unrunnable.tool` |
| `L3_install.dl`: named/grouped/single/off/active formats | `Named.grouped`, `In.grouped`, `.single`, `Off.omitted`, `.skipped`, `Active.available` |
| Key/value-spelling existence; three `export_spelled` clauses | `HasSpelling.spelled`, `.unspelled`, `HasValueSpelling.entry`; `Spelled.enumerated`, `.plainValue`, `.nonValue` |
| Property loss and warning; foreign shipped-rule loss and warning | `Loss.property`, `PropertyWarning.loss`, `RulesLoss.foreign`, `RulesWarning.loss`; losses retain format-group indexing |
| Both overlap errors and missing key/value spelling errors | `Error.bothSpelled`, `.bothValueSpelled`, `.missingKey`, `.missingValue`; export-error theorem conclusions assert error existence, not formatted message identity |

### Precisely what is and is not covered

* The tool-reference result covers **scalar and list typed placeholders**,
  every argument position, and both run/load uses. The frontend's lowering
  into `action_run` / `action_run_item` facts is not verified.
* Raw paths, scripts and untyped action text are outside the positive
  dependency proof (§3.6). The separate raw-argv diagnostic for known
  foreign artifacts is not transcribed.
* `exec_config` is not postulated functional: functionality/idempotence
  follow from the shipped diagnostic slice and name existence. Its
  newer host/minimum-based functionality proof needs **no names**.
  Totality is proved on the checked declaration model below; it is not
  asserted for arbitrary `ConfigInput` records with unconstrained frozen
  base/decl-settings fields.
* The toolchain proofs concern the actual `tc_candidate`/`tc_select`
  policy and `tc_for` mapping. Probe-derived capability truth, complete
  language-demand discovery, other built-toolchain target overrides and
  full target-action ownership construction are boundary inputs.
* `otherNeeded` freezes requests, ordinary/runtime dependencies, tests and
  linkage-instance contributions. Those boundary clauses are snapshotted
  but are not all transcribed. The exactness theorems characterize **direct
  tool-induced edges**, not a global “needed iff executed” claim.
* Built-toolchain dependencies demand **all roles** of a needed owner's
  associated built toolchain. A supplied `action_tc_tool` path fact alone
  establishes host artifact selection, not that association or tool need.
* Naming preserves scoped/project/unique-candidate cases and least alias
  closure. Qualification, `id_scope` extraction, argv string rendering and artifact production
  remain boundary computations. Inputs are completed relation snapshots;
  they are not all claimed to be extensional or strictly lower-stratum.
* Export coverage starts from an **existing, already relocated
  `export_fact` row**, and an active **component-capable file format**.
  It does not establish own-row completeness, requirement completeness,
  relocation correctness, emitted-file correctness or importer inversion.
  Spelling is not suppressed by the overlap errors, so erroneous models
  do not have exclusive classification.
* `error_free_lossless_properties_spelled` requires globally no transcribed
  export error **and** no property loss. This is stronger than a
  per-component success condition and is stated explicitly. The newer
  `error_free_E1_row_spelled` localizes no-loss to this property's reachable
  format groups and retains its key/value; error freedom is still explicit.
* `export_usage_roundtrip` is an abstract set-level propagation theorem:
  imported own rows must be exactly relocated built own rows and
  requirement edges must match. It permits cycles by induction on finite
  least derivations. E4/E5 must justify that these constitute the complete
  supported slice. It proves neither ordering, commands nor binary
  equivalence, and is not a complete mechanization of `prop:export`.

### Phase 2b: checked declarations and total execution

`Core/Declarations.lean` replaces the two frozen boundaries with a concrete
`DeclarationInput.model`: its `baseConfig` and `declSettings` are the
following inductive shipped-rule definitions. Original policy/host/name
fields are preserved; the old record's opaque base/decl-settings fields
are not used to assume the conclusion.

| Shipped L1 relation / validation | New Lean correspondence |
|---|---|
| `decl_has`; explicit/default `decl_value` | `Has.stated`; `DeclValue.explicit`, `.default` |
| Priority/name `setting_before`; counted `setting_rank` | `Before.priority`, `.name`; `Rank.counted`; `rank_declared` retains the primitive key declaration |
| Ordinal-minimum `setting_at`, `decl_value1` | `At.minimum` over derived ranks; `Value1.minimum`; each retains its chosen row and minimality |
| Terminal/recursive `decl_suffix`; `decl_cfg` | `Suffix.nil`, `.cons`; `DeclCfg.record` |
| `mconfig`, `config_slice`; both `base_config` clauses | `Multi.declared`, `Slice.platform`; `Base.concrete`, `.sliced` |
| Declared settings and every tail | `DeclSettings.declared`, `.tail` |
| Both `has_exec_policy` clauses | `HasPolicy.inherit`, `.fixed` |
| Missing default/policy, mixed or double default policies, invalid default | `SettingsError.missingDefault`, `.missingPolicy`, `.mixedPolicy`, `.doublePolicy`, `.defaultOutside` |
| Unknown-key / out-of-domain / duplicate execution override | `SettingsError.unknownOverride`, `.outsideOverride`, `.doubleOverride` |
| Reserved `"inherit"` setting-domain value | `SettingsError.reservedValue`; absence on checked inputs proved by `error_free_reserved_absent` |
| One key declared at two priorities | `SettingsError.doublePosition`; `key_priority_unique` |
| Existing not-idempotent execution error | `ConfigInput.Diagnostic.notIdempotent`; supplies output self-rows used in the new proof |

The proof chain is:

1. Declaration fold provenance proves every base-config entry has a
   `setting_key` row and an in-domain value. Explicit values have the
   source's domain guard; invalid defaults would derive the actual default
   error. Slices preserve the declaration's settings.
2. Actual `decl_settings` rules supply the original list and every tail.
   A clean `exec_bad` is impossible because every constructor derives one
   of the transcribed errors. Missing policies and invalid overrides are
   likewise ruled out by their source checks. Thus every entry has an
   `exec_value` row.
3. Strong induction on a witness ordinal proves every nonempty value group
   has a minimum; ordinal injection proves that minimum unique. **Raw
   `exec_value` functionality is not assumed**, even on error-free inputs.
   The historical `"inherit"` example above motivated this conservative
   proof; the new override guard also proves inheritance preservation.
4. Structural list induction constructs `exec_settings`. A present host
   then constructs `exec_config` for every base configuration.
5. Absence of the actual not-idempotent diagnostic supplies
   `exec_value(k,x,x)` for a selected output x. Fixed-policy alternatives
   transfer back to the original value group; inherited alternatives are
   self-rows. Hence the selected ordinal minimum is idempotent.
6. That minimum property, list-map functionality, host uniqueness and the
   reflected-image rule prove **whole `exec_config` functionality without
   configuration names**. Base existence plus functionality gives exactly
   one destination; exec-generated configurations have their reflected
   self-destination.

`ExecutionContext.Configuration` is the subtype of those known
configurations. `tool(c)` is a noncomputable choice of the unique destination;
`tool_spec`, `tool_unique`, `tool_host` and `tool_idempotent` make its semantics
explicit. The choice cannot introduce nondeterminism because of uniqueness.
`ToolInput.tool_configuration_functional` proves the functional `prop:exec`
for compatible tool inputs, with no separate execution-existence premise.
It retains the typed-reference/resolution scope of Phase 2.

Main advanced during Phase 3: commit `80077af` replaces raw key slots with
canonical `(priority, key name)` ordering. Its update is consumed by the
merge into `lean`; `Before` and `Rank` transcribe all new defining clauses.
`At` now selects by derived rank, and rank provenance preserves every existing
entry-validity, totality and functional-tool proof. A tied-priority fixture
checks that both keys occur in name order and retain their inherited values.

The key count `nkeys`, predecessor aggregate `count` and numerical successor
`j=i+1` are completed numeric boundaries (`nkeys`, `count`, `next`). This development does not verify
Soufflé's counting/arithmetic implementation; the provenance/domain proof
works for **any** such boundary results and does not require their
correctness as an extra totality premise. It does not assert that every
arbitrary declaration produces a base configuration. It proves the
requested statement for **every already derived base configuration**.
Unique/present host and error freedom are explicit hypotheses, not claimed
as consequences of core rule evaluation.

No additional cause of silent missing execution was found under those
hypotheses. Without a host, the configuration-only fixture has a base
configuration, no exec destination and no error; this is outside the
present-host hypothesis. The historical inheritance-policy bug above left
a unique destination; it is now rejected and its override semantics corrected.

### Phase 2b: row-specific export exclusivity and E1

`RowSpelled` is a provenance refinement of the three actual `Spelled`
clauses: it retains the original key/value. `row_spelled_projects` proves
that it really produces a core spelling. On error-free models an existing
row in an active component-capable file format is **exactly one of**
row-spelled or declared unspelled **in that same file**. The second branch
produces the actual group-indexed loss and warning.

This intentionally does not assert that a bare `Spelled` tuple and a bare
group-level `Loss` tuple are mutually exclusive: the former drops the
original key, and the latter drops the producing file. In particular, two
different files in one format group can spell/reject the same key without
an overlap error. The exclusivity result preserves the witnesses needed
to classify the specific row/file; it does not silently conflate those
projections. Own-row completeness, relocation and requirements remain
outside this exporter subset.

For the downstream proof, the precise corrected premise is:
**(E1′) successful/error-free export, and no loss for the component and its
requirements in the relevant format.** `error_free_E1_row_spelled` proves
the spelling consequence locally for a given row. The finite one-row
theorem `no_loss_alone_insufficient` establishes that plain E1 cannot
replace E1′: missing spelling produces an error but no loss. Neither
theorem verifies actual importers or derives the remaining E2–E5.

### Automated drift detection and maintenance

From the repository root:

```sh
python formal/lean/scripts/check_core_rules.py
python -m pytest -n2 -q tests/test_lean_core_rules.py \
  --basetemp=formal/lean/.pytest-runtime
BUILDLOG_SOUFFLE=/opt/homebrew/bin/souffle python -m pytest -n2 -q \
  tests/test_lean_core_rules.py tests/test_lean_exec_totality.py \
  --basetemp=formal/lean/.pytest-runtime
BUILDLOG_SOUFFLE=/opt/homebrew/bin/souffle python -m pytest -n2 -q \
  tests/test_lean_core_rules.py tests/test_core_invariants_prop.py \
  --basetemp=formal/lean/.pytest-runtime
```

Use the project's Python environment; replace the Soufflé path as appropriate.
The property suite otherwise skips runtime cases when `BUILDLOG_SOUFFLE`
is unset. Drift checks run without Lean or Soufflé.

`scripts/check_core_rules.py` scans **all core `.dl` files**, not selected
line ranges. It snapshots complete defining clauses for protected
relations, the selected diagnostics and typed path multi-head clauses,
relevant declarations and the `Setting`/`Settings`/`Cfg` record types.
All `needed` and configuration-boundary clauses are included even where
their contribution is abstracted. The checked-in snapshot has **201 entries:
111 clauses, 87 declarations, 3 types**; the core-only snapshot at commit
`2bcd74b` had 118 entries (65/50/3).

Comments/layout are ignored; strings, head arguments, guards, aggregates
and operators are retained. Tests cover a changed premise, a new source
file, a new second head, comment/layout/string handling, and mandatory
coverage of both pin diagnostics, lists, built roles, minima, configuration
closure and exports. Changes to existing schemas or unsupported declaration
layouts fail the snapshot check.

This is **drift detection plus manual correspondence**, not a verified
Soufflé parser/translator, a proof that Python reads every possible syntax,
or an automatic equivalence theorem connecting the whole engine to Lean.
Non-core extension rules, frontend code and all unselected relations are
outside this checker. When selected sources change, review every affected
constructor, proof and boundary assumption, update this correspondence,
then deliberately regenerate with:

```sh
python formal/lean/scripts/check_core_rules.py --write-snapshot
```

Never update the expected text merely to silence a drift failure.

Phase-2 validation: all **245** drift and shipped core property tests
passed with Soufflé 2.5 (64-bit), using the combined command above (334.94 s).
No runtime property cases were skipped in that run.
Both `lake clean && lake build` and a separate build of the committed
`formal/lean` directory extracted with `git archive` passed (19 jobs),
including every headline axiom audit.

Phase-2b validation: **19 tests passed** (4 drift tests and 15 direct
shipped-rule totality/error fixtures), with no skips, in 3.20 s. Cases cover
inherit/fixed policies and overrides, multi-platform slices, empty settings,
no host, missing/conflicting policies, duplicate/invalid overrides, invalid
defaults/fixed values, and the silent `"inherit"` collision. Both a clean
Lake rebuild and a clean committed-directory archive build passed
**22 jobs**, including all new axiom audits.

## Completion report

The project contains **3,782 Lean source lines**, counting the root import
file, examples, comments, and axiom audit (not Lake-generated files).

Proved without admissions: finite progress/stratum construction, abstract
fresh termination and determinism, both inertness orientations, guarded
least closure and uniqueness, normalized restart termination/leastness/
grouping independence, reason-stutter progress, and answer-free
dependency-certificate soundness. The phase conclusion and error
noninterference/final-error reductions are also checked, with their extra
semantic hypotheses stated above.

Phase 2 additionally proves shipped toolchain selection/conflict coverage;
host execution destinations and conditional functionality/idempotence;
resolved scalar/list typed-tool dependencies and host-artifact lowering;
built-toolchain role dependencies; and leastness of the mutually recursive
needed/dependency slice. Optional export work proves property-row coverage,
loss/warning and overlap diagnostics, the error-free/lossless spelling
consequence, and conditional abstract usage reconstruction.

Phase 2b proves declaration provenance, total and unique execution
destinations on checked inputs, a host-based functionality proof requiring
no names, functional/idempotent `tool(c)`, functional typed-tool need,
row-specific error-free export exclusivity and the corrected E1 implication.
It also supplies a finite checked counterexample to plain E1. The historical
inheritance/domain-value collision is resolved by main's `a5685e4`, with its
new guard and validation error transcribed and checked here.

Phase 3 proves FC/FL active-answer correctness, the finite row/bundle boundary,
phase reduction with arbitrary wrong inactive seeds, package-schedule
termination and agreement, and cached/fresh outcome equality for certified
completed phase runs and replay provenance. Operational certificate extraction
and the actual Python seed-loop termination remain open. It also discharges
W4 dependency isolation and final completion gates for ground-rule programs,
deriving equal driver traces/checks and final errors/attributions.

Open: general-F grounding reduction; real loader/auxiliary-guard extraction;
real Python α/β/row interpretation; shipped fixed-answer
and term-finiteness arguments; full operational replay/seeding verification; execution
context extraction/validation for the real driver and complete frontend/boundary extraction; full exporter
completeness, relocation and concrete importer inversion. **There are no marked or hidden `sorry`s
for these items.** They are unclaimed obligations, not completed theorems.

Inspect the complete development history with:

```sh
git log --oneline -- formal/lean
```

Each development commit includes the requested Copilot co-author trailer.
