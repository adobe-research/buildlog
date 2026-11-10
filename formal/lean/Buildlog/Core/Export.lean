-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Core.Configuration

namespace Buildlog.Core

/-- Completed export facts already contain relocated path values. Computing
own facts, relocation, requirements and writing/parsing files are not assumed
correct merely by making this rule-input record. -/
structure ExportInput where
  valueKind : Symbol
  buildlogGroup : Symbol
  fact : Symbol → Symbol → Config → Symbol → Symbol → Symbol → Prop
  formatFile : Symbol → Symbol → Symbol → Symbol → Prop
  formatOf : Symbol → Symbol → Prop
  formatOmit : Symbol → Symbol → Symbol → Prop
  skip : Symbol → Symbol → Prop
  components : Symbol → Prop
  spelling : Symbol → Symbol → Symbol → Prop
  valueSpelling : Symbol → Symbol → Symbol → Symbol → Prop
  unspelled : Symbol → Symbol → Symbol → Prop
  rules : Symbol → Symbol → Prop

namespace ExportInput

inductive Named (i : ExportInput) : Symbol → Prop
  | grouped : i.formatOf f group → Named i f

inductive In (i : ExportInput) : Symbol → Symbol → Prop
  | grouped : i.formatOf f group → In i f group
  | single : i.formatFile p f dir name → ¬ Named i f → In i f f

inductive Off (i : ExportInput) : Symbol → Symbol → Prop
  | omitted : i.formatOmit p group why → Off i p group
  | skipped : i.skip p group → Off i p group

inductive Active (i : ExportInput) : Symbol → Symbol → Prop
  | available : i.formatFile p f dir name → In i f group → ¬ Off i p group → Active i p f

inductive HasValueSpelling (i : ExportInput) : Symbol → Symbol → Prop
  | entry : i.valueSpelling f k v word → HasValueSpelling i f k

inductive HasSpelling (i : ExportInput) : Symbol → Symbol → Prop
  | spelled : i.spelling f k field → HasSpelling i f k
  | unspelled : i.unspelled f k why → HasSpelling i f k

inductive Spelled (i : ExportInput) :
    Symbol → Symbol → Config → Symbol → Symbol → Symbol → Symbol → Prop
  | enumerated : i.fact p m c i.valueKind k v → i.components f →
      i.spelling f k field → i.valueSpelling f k v word →
      Spelled i p m c f i.valueKind field word
  | plainValue : i.fact p m c i.valueKind k v → i.components f →
      i.spelling f k field → ¬ HasValueSpelling i f k →
      Spelled i p m c f i.valueKind field v
  | nonValue : i.fact p m c kind k v → kind ≠ i.valueKind → i.components f →
      i.spelling f k field → Spelled i p m c f kind field v

inductive Loss (i : ExportInput) : Symbol → Symbol → Symbol → Symbol → Symbol → Prop
  | property : i.fact p m c kind k v → Active i p f → In i f group →
      i.unspelled f k why → Loss i p group m k why

inductive PropertyWarning (i : ExportInput) : Symbol → Symbol → Symbol → Symbol → Symbol → Prop
  | loss : Loss i p group m k why → PropertyWarning i p group m k why

/-- The two overlap diagnostics are those landed independently in main.
They do not suppress spelling/loss facts in erroneous models. -/
inductive Error (i : ExportInput) : Prop
  | bothSpelled : i.spelling f k field → i.unspelled f k why → Error i
  | bothValueSpelled : i.valueSpelling f k v word → i.unspelled f k why → Error i
  | missingKey : i.fact p m c kind k v → Active i p f → i.components f →
      ¬ HasSpelling i f k → Error i
  | missingValue : i.fact p m c i.valueKind k v → Active i p f →
      HasValueSpelling i f k → (∀ word, ¬ i.valueSpelling f k v word) → Error i

inductive RulesLoss (i : ExportInput) : Symbol → Symbol → Symbol → Prop
  | foreign : i.rules p dir → Active i p f → In i f group →
      group ≠ i.buildlogGroup → RulesLoss i p group dir

inductive RulesWarning (i : ExportInput) : Symbol → Symbol → Symbol → Prop
  | loss : RulesLoss i p group dir → RulesWarning i p group dir

/-- Corrected coverage, not exclusive classification. Scope is a property row
of an active component-capable FILE format; group-level losses are retained.
Relocation and completeness of export_fact relative to own are separate. -/
theorem export_property_coverage (i : ExportInput)
    {p m f kind k v : Symbol} {c : Config}
    (fact : i.fact p m c kind k v) (active : Active i p f) (componentFormat : i.components f) :
    (∃ field word, Spelled i p m c f kind field word) ∨
      (∃ group why, In i f group ∧ Loss i p group m k why ∧
        PropertyWarning i p group m k why) ∨ Error i := by
  classical
  by_cases spelling : ∃ field, i.spelling f k field
  · obtain ⟨field, spelling⟩ := spelling
    by_cases isValue : kind = i.valueKind
    · subst kind
      by_cases enumerated : HasValueSpelling i f k
      · by_cases hasWord : ∃ word, i.valueSpelling f k v word
        · obtain ⟨word, hw⟩ := hasWord
          exact Or.inl ⟨field, word, Spelled.enumerated fact componentFormat spelling hw⟩
        · exact Or.inr (Or.inr (Error.missingValue fact active enumerated
            (fun word hw => hasWord ⟨word, hw⟩)))
      · exact Or.inl ⟨field, v, Spelled.plainValue fact componentFormat spelling enumerated⟩
    · exact Or.inl ⟨field, v, Spelled.nonValue fact isValue componentFormat spelling⟩
  · by_cases unspelled : ∃ why, i.unspelled f k why
    · obtain ⟨why, hu⟩ := unspelled
      cases active with
      | available file group on =>
        have active := Active.available file group on
        have loss := Loss.property fact active group hu
        exact Or.inr (Or.inl ⟨_, why, group, loss, PropertyWarning.loss loss⟩)
    · right; right
      apply Error.missingKey fact active componentFormat
      intro has
      cases has with
      | spelled h => exact spelling ⟨_, h⟩
      | unspelled h => exact unspelled ⟨_, h⟩

/-- The strengthened success premise needed in the downstream-use proof:
no loss alone does not eliminate the export-error alternative. -/
theorem error_free_lossless_properties_spelled (i : ExportInput)
    (clean : ¬ Error i)
    (lossless : ∀ p group m k why, ¬ Loss i p group m k why)
    {p m f kind k v : Symbol} {c : Config}
    (fact : i.fact p m c kind k v) (active : Active i p f) (componentFormat : i.components f) :
    ∃ field word, Spelled i p m c f kind field word := by
  rcases i.export_property_coverage fact active componentFormat with spelled | lost | error
  · exact spelled
  · obtain ⟨group, why, _, hl, _⟩ := lost
    exact False.elim (lossless p group m k why hl)
  · exact False.elim (clean error)

theorem overlapping_tables_error (i : ExportInput) {f k field why : Symbol}
    (spells : i.spelling f k field) (rejects : i.unspelled f k why) : Error i :=
  Error.bothSpelled spells rejects

theorem foreign_rules_lost_with_warning (i : ExportInput) {p f group dir : Symbol}
    (rules : i.rules p dir) (active : Active i p f) (formatGroup : In i f group)
    (foreign : group ≠ i.buildlogGroup) :
    RulesLoss i p group dir ∧ RulesWarning i p group dir :=
  ⟨RulesLoss.foreign rules active formatGroup foreign,
    RulesWarning.loss (RulesLoss.foreign rules active formatGroup foreign)⟩

/-- Provenance refinement of Spelled: retain the original key/value rather
than confusing a spelling of another property with a spelling of this row.
The three constructors project to precisely the three shipped clauses. -/
inductive RowSpelled (i : ExportInput) :
    Symbol → Symbol → Config → Symbol → Symbol → Symbol → Symbol → Prop
  | enumerated : i.fact p m c i.valueKind k v → i.components f →
      i.spelling f k field → i.valueSpelling f k v word →
      RowSpelled i p m c f i.valueKind k v
  | plainValue : i.fact p m c i.valueKind k v → i.components f →
      i.spelling f k field → ¬ HasValueSpelling i f k →
      RowSpelled i p m c f i.valueKind k v
  | nonValue : i.fact p m c kind k v → kind ≠ i.valueKind → i.components f →
      i.spelling f k field → RowSpelled i p m c f kind k v

theorem row_spelled_projects (i : ExportInput) {p m f kind k v : Symbol} {c : Config}
    (row : RowSpelled i p m c f kind k v) :
    ∃ field word, Spelled i p m c f kind field word := by
  cases row with
  | enumerated fact components spelling value =>
    exact ⟨_, _, Spelled.enumerated fact components spelling value⟩
  | plainValue fact components spelling plain =>
    exact ⟨_, _, Spelled.plainValue fact components spelling plain⟩
  | nonValue fact nonValue components spelling =>
    exact ⟨_, _, Spelled.nonValue fact nonValue components spelling⟩

theorem rejected_row_lost_with_warning (i : ExportInput)
    {p m f kind k v : Symbol} {c : Config}
    (fact : i.fact p m c kind k v) (active : Active i p f)
    (rejected : ∃ why, i.unspelled f k why) :
    ∃ group why, In i f group ∧ Loss i p group m k why ∧
      PropertyWarning i p group m k why := by
  obtain ⟨why, entry⟩ := rejected
  cases active with
  | available file group on =>
    have loss := Loss.property fact (Active.available file group on) group entry
    exact ⟨_, why, group, loss, PropertyWarning.loss loss⟩

/-- Error-free, row-specific exclusivity. The loss branch retains its source
file's unspelled entry; Loss alone forgets that file and merges format groups. -/
theorem error_free_row_exclusive (i : ExportInput) (clean : ¬ Error i)
    {p m f kind k v : Symbol} {c : Config}
    (fact : i.fact p m c kind k v) (active : Active i p f) (components : i.components f) :
    (RowSpelled i p m c f kind k v ∨ ∃ why, i.unspelled f k why) ∧
      ¬ (RowSpelled i p m c f kind k v ∧ ∃ why, i.unspelled f k why) := by
  classical
  constructor
  · by_cases spelling : ∃ field, i.spelling f k field
    · obtain ⟨field, entry⟩ := spelling
      left
      by_cases valueKind : kind = i.valueKind
      · subst kind
        by_cases enumerated : HasValueSpelling i f k
        · have word : ∃ word, i.valueSpelling f k v word := by
            apply Classical.byContradiction
            intro missing
            exact clean (Error.missingValue fact active enumerated
              (fun word row => missing ⟨word, row⟩))
          obtain ⟨word, row⟩ := word
          exact RowSpelled.enumerated fact components entry row
        · exact RowSpelled.plainValue fact components entry enumerated
      · exact RowSpelled.nonValue fact valueKind components entry
    · right
      apply Classical.byContradiction
      intro missing
      apply clean
      apply Error.missingKey fact active components
      intro has
      cases has with
      | spelled row => exact spelling ⟨_, row⟩
      | unspelled row => exact missing ⟨_, row⟩
  · rintro ⟨spelled, why, rejected⟩
    cases spelled with
    | enumerated _ _ spelling _ => exact clean (Error.bothSpelled spelling rejected)
    | plainValue _ _ spelling _ => exact clean (Error.bothSpelled spelling rejected)
    | nonValue _ _ _ spelling => exact clean (Error.bothSpelled spelling rejected)

/-- E1's local no-loss premise must be accompanied by error-free export.
The conclusion preserves the original property, not merely some component
spelling. Completeness/relocation/import inversion remain separate. -/
theorem error_free_E1_row_spelled (i : ExportInput) (clean : ¬ Error i)
    {p m f kind k v : Symbol} {c : Config}
    (lossless : ∀ group why, In i f group → ¬ Loss i p group m k why)
    (fact : i.fact p m c kind k v) (active : Active i p f) (components : i.components f) :
    RowSpelled i p m c f kind k v := by
  rcases (i.error_free_row_exclusive clean fact active components).1 with spelled | rejected
  · exact spelled
  · obtain ⟨group, why, belongs, lost, _⟩ :=
      i.rejected_row_lost_with_warning fact active rejected
    exact False.elim (lossless group why belongs lost)

/-- A finite one-row counterexample to E1 alone: missing spelling is an
export error, not a loss. No concrete importer claim is involved. -/
theorem no_loss_alone_insufficient :
    ∃ i : ExportInput,
      (∀ p group m k why, ¬ Loss i p group m k why) ∧
      i.fact 0 0 ⟨0, []⟩ 1 0 0 ∧ Active i 0 0 ∧ i.components 0 ∧
      ¬ RowSpelled i 0 0 ⟨0, []⟩ 0 1 0 0 ∧ Error i := by
  let i : ExportInput := {
    valueKind := 1
    buildlogGroup := 2
    fact := fun p m c kind k v =>
      p = 0 ∧ m = 0 ∧ c = ⟨0, []⟩ ∧ kind = 1 ∧ k = 0 ∧ v = 0
    formatFile := fun p f dir name => p = 0 ∧ f = 0 ∧ dir = 0 ∧ name = 0
    formatOf := fun _ _ => False
    formatOmit := fun _ _ _ => False
    skip := fun _ _ => False
    components := fun f => f = 0
    spelling := fun _ _ _ => False
    valueSpelling := fun _ _ _ _ => False
    unspelled := fun _ _ _ => False
    rules := fun _ _ => False }
  have file : i.formatFile 0 0 0 0 := ⟨rfl, rfl, rfl, rfl⟩
  have notNamed : ¬ Named i 0 := by
    intro named
    cases named with
    | grouped entry => cases entry
  have on : ¬ Off i 0 0 := by
    intro off
    cases off with
    | omitted entry => cases entry
    | skipped entry => cases entry
  have fact : i.fact 0 0 ⟨0, []⟩ 1 0 0 := ⟨rfl, rfl, rfl, rfl, rfl, rfl⟩
  have active := Active.available file (In.single file notNamed) on
  have missing : ¬ HasSpelling i 0 0 := by
    intro has
    cases has with
    | spelled entry => cases entry
    | unspelled entry => cases entry
  refine ⟨i, ?_, fact, active, rfl, ?_, Error.missingKey fact active rfl missing⟩
  · intro p group m k why loss
    cases loss with
    | property _ _ _ entry => cases entry
  · intro row
    cases row with
    | enumerated _ _ entry _ => cases entry
    | plainValue _ _ entry _ => cases entry
    | nonValue _ _ _ entry => cases entry

end ExportInput

/-- Set-level dependency usage, abstracting the supported propagation slice.
Components already include the matched configuration/resolution (E3).
Induction is on least derivations, not an assumed acyclic requirement graph. -/
inductive Usage {Fact : Type} (own : Symbol → Fact → Prop)
    (edges : Symbol → Symbol → Prop) : Symbol → Fact → Prop
  | own : own t fact → Usage own edges t fact
  | dependency : edges t dep → Usage own edges dep fact → Usage own edges t fact

/-- Conditional downstream reconstruction: E2 plus successful, complete
exports establishes `ownRows`; E3 establishes `requirements`. E4/E5 must
justify that these are ALL inputs/rules of the relevant propagation slice.
No concrete importer, ordering, command or binary equivalence is claimed. -/
theorem export_usage_roundtrip {BuiltFact ImportedFact : Type}
    (builtOwn : Symbol → BuiltFact → Prop) (importedOwn : Symbol → ImportedFact → Prop)
    (builtReq importedReq : Symbol → Symbol → Prop) (loc : BuiltFact → ImportedFact)
    (ownRows : ∀ t word, importedOwn t word ↔ ∃ fact, builtOwn t fact ∧ loc fact = word)
    (requirements : ∀ t dep, builtReq t dep ↔ importedReq t dep)
    (t : Symbol) (word : ImportedFact) :
    Usage importedOwn importedReq t word ↔
      ∃ fact, Usage builtOwn builtReq t fact ∧ loc fact = word := by
  constructor
  · intro use
    induction use with
    | own h =>
      obtain ⟨fact, hf, eq⟩ := (ownRows _ _).mp h
      exact ⟨fact, Usage.own hf, eq⟩
    | dependency edge _ ih =>
      obtain ⟨fact, hf, eq⟩ := ih
      exact ⟨fact, Usage.dependency ((requirements _ _).mpr edge) hf, eq⟩
  · rintro ⟨fact, use, eq⟩
    subst word
    induction use with
    | own h => exact Usage.own ((ownRows _ _).mpr ⟨_, h, rfl⟩)
    | dependency edge _ ih => exact Usage.dependency ((requirements _ _).mp edge) ih

end Buildlog.Core
