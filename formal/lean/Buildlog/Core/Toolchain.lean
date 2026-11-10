-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Core.Configuration

namespace Buildlog.Core

/-- Open capability/policy/action relations at a completed boundary
snapshot. Configuration relations below are the transcribed core relations. -/
structure ToolchainInput where
  config : ConfigInput
  runsOn : Symbol → Symbol → Prop
  canTarget : Symbol → Symbol → Symbol → Prop
  toolTarget : Symbol → Symbol → Symbol → Prop
  pin : Symbol → Symbol → Symbol → Symbol → Prop
  required : Symbol → Config → Prop
  targetAction : Symbol → Config → Symbol → Prop
  authorPinReason : Symbol → Symbol → Symbol → Symbol → Prop

namespace ToolchainInput

-- One constructor per clause in L2_toolchains.dl.
inductive Built (i : ToolchainInput) : Symbol → Prop
  | tool : i.toolTarget tc role target → Built i tc

inductive Candidate (i : ToolchainInput) : Symbol → Symbol → Symbol → Symbol → Prop
  | available : i.runsOn tc e → i.canTarget tc l p → ¬ Built i tc → Candidate i l e p tc

inductive Pinned (i : ToolchainInput) : Symbol → Symbol → Symbol → Prop
  | pin : i.pin l e p tc → Pinned i l e p

inductive Select (i : ToolchainInput) : Symbol → Symbol → Symbol → Symbol → Prop
  | unpinned : Candidate i l e p tc → ¬ Pinned i l e p → Select i l e p tc
  | pinned : Candidate i l e p tc → i.pin l e p tc → Select i l e p tc

inductive Used (i : ToolchainInput) : Symbol → Symbol → Symbol → Prop
  | required : i.required l c → ConfigInput.Platform i.config c p →
      ConfigInput.Exec i.config c x → ConfigInput.Platform i.config x e → Used i l e p

inductive For (i : ToolchainInput) : Symbol → Config → Symbol → Prop
  | selected : ConfigInput.Platform i.config c p → ConfigInput.Exec i.config c x →
      ConfigInput.Platform i.config x e → Select i l e p tc → For i l c tc

inductive Has (i : ToolchainInput) : Symbol → Config → Prop
  | selected : For i l c tc → Has i l c

/-- Structured diagnostics retain sorted witnesses and both author_decision
branches. Only literal diagnostic text/concatenation is erased. -/
inductive Error (i : ToolchainInput) : Symbol → Symbol → Symbol → Prop
  | ambiguous : Used i l e p → Select i l e p a → Select i l e p b →
      a < b → Error i l e p
  | badPin : i.pin l e p tc → Used i l e p → ¬ Candidate i l e p tc → Error i l e p
  | doublePin : i.pin l e p a → i.pin l e p b → a < b →
      (∀ reason, ¬ i.authorPinReason l e p reason) → Error i l e p
  | authorDoublePin : i.pin l e p a → i.pin l e p b → a < b →
      i.authorPinReason l e p reason → Error i l e p

inductive NoToolchain (i : ToolchainInput) : Symbol → Symbol → Config → Prop
  | target : i.required l c → ¬ Has i l c → i.config.cfgName c n →
      i.targetAction t c action → NoToolchain i l t c

theorem selection_exact (i : ToolchainInput) {l e p tc : Symbol} :
    Select i l e p tc ↔ Candidate i l e p tc ∧ (¬ Pinned i l e p ∨ i.pin l e p tc) := by
  constructor
  · intro h
    cases h with
    | unpinned hc hn => exact ⟨hc, Or.inl hn⟩
    | pinned hc hp => exact ⟨hc, Or.inr hp⟩
  · rintro ⟨hc, hn | hp⟩
    · exact Select.unpinned hc hn
    · exact Select.pinned hc hp

theorem unpinned_selects_all (i : ToolchainInput) {l e p tc : Symbol}
    (hn : ¬ Pinned i l e p) : Select i l e p tc ↔ Candidate i l e p tc := by
  constructor
  · intro h; exact (i.selection_exact.mp h).1
  · intro h; exact Select.unpinned h hn

theorem pinned_selects_only_pin (i : ToolchainInput) {l e p tc : Symbol}
    (hp : Pinned i l e p) :
    Select i l e p tc ↔ Candidate i l e p tc ∧ i.pin l e p tc := by
  constructor
  · intro h
    obtain ⟨hc, hn | pin⟩ := i.selection_exact.mp h
    · exact False.elim (hn hp)
    · exact ⟨hc, pin⟩
  · rintro ⟨hc, pin⟩; exact Select.pinned hc pin

theorem selection_has_capabilities (i : ToolchainInput) {l e p tc : Symbol}
    (h : Select i l e p tc) :
    i.runsOn tc e ∧ i.canTarget tc l p ∧ ¬ Built i tc := by
  have hc := (i.selection_exact.mp h).1
  cases hc with
  | available runs can notBuilt => exact ⟨runs, can, notBuilt⟩

theorem multiple_selected_error (i : ToolchainInput) {l e p a b : Symbol}
    (used : Used i l e p) (ha : Select i l e p a) (hb : Select i l e p b)
    (different : a ≠ b) : Error i l e p := by
  rcases Nat.lt_or_gt_of_ne different with lt | gt
  · exact Error.ambiguous used ha hb lt
  · exact Error.ambiguous used hb ha gt

theorem multiple_unpinned_candidates_error (i : ToolchainInput) {l e p a b : Symbol}
    (used : Used i l e p) (unpinned : ¬ Pinned i l e p)
    (ha : Candidate i l e p a) (hb : Candidate i l e p b) (different : a ≠ b) :
    Error i l e p :=
  i.multiple_selected_error used (Select.unpinned ha unpinned)
    (Select.unpinned hb unpinned) different

/-- Pin conflicts are errors even for unused triples and even when the
author declared the decision. The source has two diagnostic clauses. -/
theorem conflicting_pins_error (i : ToolchainInput) {l e p a b : Symbol}
    (ha : i.pin l e p a) (hb : i.pin l e p b) (different : a ≠ b) : Error i l e p := by
  classical
  have sorted : ∀ a b, i.pin l e p a → i.pin l e p b → a < b → Error i l e p := by
    intro a b pa pb order
    by_cases author : ∃ reason, i.authorPinReason l e p reason
    · obtain ⟨reason, hr⟩ := author
      exact Error.authorDoublePin pa pb order hr
    · exact Error.doublePin pa pb order (fun reason hr => author ⟨reason, hr⟩)
  rcases Nat.lt_or_gt_of_ne different with lt | gt
  · exact sorted a b ha hb lt
  · exact sorted b a hb ha gt

theorem noncandidate_pin_error (i : ToolchainInput) {l e p tc : Symbol}
    (used : Used i l e p) (pin : i.pin l e p tc) (bad : ¬ Candidate i l e p tc) :
    Error i l e p := Error.badPin pin used bad

/-- The paper's "otherwise at most one": these are the negations of the
reported conflict cases, not an assumed functional selection relation. -/
theorem toolchain_uniqueness (i : ToolchainInput) {l e p : Symbol}
    (unambiguous : ¬ Pinned i l e p →
      ∀ a b, Candidate i l e p a → Candidate i l e p b → a = b)
    (consistentPins : ∀ a b, i.pin l e p a → i.pin l e p b → a = b) :
    ∀ a b, Select i l e p a → Select i l e p b → a = b := by
  classical
  intro a b ha hb
  by_cases pinned : Pinned i l e p
  · exact consistentPins a b ((i.pinned_selects_only_pin pinned).mp ha).2
      ((i.pinned_selects_only_pin pinned).mp hb).2
  · exact unambiguous pinned a b (i.selection_exact.mp ha).1 (i.selection_exact.mp hb).1

/-- Stronger error-free form, allowing arbitrary numbers of candidates and
pins: on a used triple either selection is functional or an error exists. -/
theorem no_silent_choice (i : ToolchainInput) {l e p : Symbol}
    (used : Used i l e p) :
    Error i l e p ∨ ∀ a b, Select i l e p a → Select i l e p b → a = b := by
  classical
  by_cases bad : Error i l e p
  · exact Or.inl bad
  · right
    intro a b ha hb
    apply Classical.byContradiction
    intro different
    exact bad (i.multiple_selected_error used ha hb different)

theorem no_selection_no_for (i : ToolchainInput) {l : Symbol} {c x : Config}
    (unique : ∀ y, ConfigInput.Exec i.config c y → y = x)
    (none : ∀ tc, ¬ Select i l x.platform c.platform tc) : ¬ Has i l c := by
  intro has
  cases has with
  | selected selectedFor =>
    cases selectedFor with
    | selected platform exec execPlatform selected =>
      have targetEq := i.config.platform_eq platform
      have execEq := unique _ exec
      have platformEq := i.config.platform_eq execPlatform
      subst_vars
      exact none _ selected

/-- Exact no-toolchain rule: required language, no tc_for selection, a name,
and every target_action owner in the configuration. -/
theorem missing_selection_errors_every_action (i : ToolchainInput) {l : Symbol} {c : Config}
    (required : i.required l c) (missing : ¬ Has i l c)
    (named : ∃ n, i.config.cfgName c n) :
    ∀ t action, i.targetAction t c action → NoToolchain i l t c := by
  obtain ⟨n, hn⟩ := named
  exact fun _ _ action => NoToolchain.target required missing hn action

/-- The end-to-end "no selection" clause, using the shipped exec diagnostics
to discharge uniqueness of the configuration's execution platform. -/
theorem no_selection_errors_every_action (i : ToolchainInput)
    (configClean : ¬ ConfigInput.Diagnostic i.config)
    (named : ∀ c, ConfigInput.Known i.config c → ∃ n, i.config.cfgName c n)
    {l : Symbol} {c x : Config} (required : i.required l c)
    (exec : ConfigInput.Exec i.config c x)
    (none : ∀ tc, ¬ Select i l x.platform c.platform tc) :
    ∀ t action, i.targetAction t c action → NoToolchain i l t c := by
  apply i.missing_selection_errors_every_action required
  · exact i.no_selection_no_for
      (fun y hy => i.config.exec_config_functional configClean named hy exec) none
  · exact named c (i.config.exec_source_known exec)

/-- Exhaustive paper-facing outcome on a required, named configuration with
an exec destination: a triple error, exactly one selection, or "no toolchain"
at every action owner. Configuration check failures are not silently ignored. -/
theorem required_selection_outcome (i : ToolchainInput)
    (configClean : ¬ ConfigInput.Diagnostic i.config)
    (named : ∀ c, ConfigInput.Known i.config c → ∃ n, i.config.cfgName c n)
    {l : Symbol} {c x : Config} (required : i.required l c)
    (exec : ConfigInput.Exec i.config c x) :
    Error i l x.platform c.platform ∨
      (∃ tc, Select i l x.platform c.platform tc ∧
        ∀ other, Select i l x.platform c.platform other → other = tc) ∨
      (∀ t action, i.targetAction t c action → NoToolchain i l t c) := by
  classical
  have used : Used i l x.platform c.platform :=
    Used.required required (i.config.known_platform (i.config.exec_source_known exec))
      exec (i.config.exec_platform_known exec)
  rcases i.no_silent_choice used with error | unique
  · exact Or.inl error
  · right
    by_cases selected : ∃ tc, Select i l x.platform c.platform tc
    · obtain ⟨tc, htc⟩ := selected
      exact Or.inl ⟨tc, htc, fun other hother => unique other tc hother htc⟩
    · exact Or.inr (i.no_selection_errors_every_action configClean named required exec
        (fun tc htc => selected ⟨tc, htc⟩))

end ToolchainInput
end Buildlog.Core
