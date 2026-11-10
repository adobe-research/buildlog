-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Core.Execution

namespace Buildlog.Core

/-- Primitive declaration/platform relations and completed numeric operations.
`nkeys` is the key count, `count` evaluates predecessor aggregates, and `next`
is the engine's j=i+1 operation. Arithmetic/count correctness is not needed
for the entry-validity proof; priority is distinct from canonical rank. -/
structure DeclarationInput where
  policy : ConfigInput
  settingKey : Symbol → Int → Prop
  settingDefault : Symbol → Symbol → Prop
  configDecl : Symbol → Symbol → Prop
  configDeclSetting : Symbol → Symbol → Symbol → Prop
  nkeys : Int → Prop
  count : (Symbol → Prop) → Int
  next : Int → Int
  platform : Symbol → Symbol → Prop
  platformSlice : Symbol → Symbol → Prop

namespace DeclarationInput

inductive Has (d : DeclarationInput) : Symbol → Symbol → Prop
  | stated : d.configDeclSetting n k v → Has d n k

inductive DeclValue (d : DeclarationInput) : Symbol → Symbol → Symbol → Prop
  | explicit : d.configDeclSetting n k v → d.policy.settingValue k v → DeclValue d n k v
  | default : d.configDecl n p → d.settingDefault k v → ¬ Has d n k → DeclValue d n k v

inductive Before (d : DeclarationInput) : Symbol → Symbol → Prop
  | priority : d.settingKey a i → d.settingKey b j → i < j → Before d a b
  | name : d.settingKey a i → d.settingKey b i → a < b → Before d a b

inductive Rank (d : DeclarationInput) : Symbol → Int → Prop
  | counted : d.settingKey k priority → Rank d k (d.count (fun x => Before d x k))

inductive At (d : DeclarationInput) : Int → Symbol → Prop
  | minimum : Rank d k pos →
      (∀ other, Rank d other pos → d.policy.ordinal k ≤ d.policy.ordinal other) →
      At d pos k

inductive Value1 (d : DeclarationInput) : Symbol → Symbol → Symbol → Prop
  | minimum : DeclValue d n k v →
      (∀ other, DeclValue d n k other → d.policy.ordinal v ≤ d.policy.ordinal other) →
      Value1 d n k v

inductive Suffix (d : DeclarationInput) : Symbol → Int → Settings → Prop
  | nil : d.configDecl n p → d.nkeys pos → Suffix d n pos []
  | cons : At d pos k → Suffix d n (d.next pos) tail → Value1 d n k v →
      Suffix d n pos ((k, v) :: tail)

inductive DeclCfg (d : DeclarationInput) : Symbol → Config → Prop
  | record : d.configDecl n p → Suffix d n 0 s → DeclCfg d n ⟨p, s⟩

inductive Multi (d : DeclarationInput) : Config → Prop
  | declared : DeclCfg d n ⟨u, s⟩ → d.platformSlice u p → Multi d ⟨u, s⟩

inductive Slice (d : DeclarationInput) : Config → Config → Prop
  | platform : Multi d ⟨u, s⟩ → d.platformSlice u p → Slice d ⟨u, s⟩ ⟨p, s⟩

inductive Base (d : DeclarationInput) : Config → Prop
  | concrete : DeclCfg d n ⟨p, s⟩ → d.platform p triple → Base d ⟨p, s⟩
  | sliced : Slice d u c → Base d c

inductive DeclSettings (d : DeclarationInput) : Settings → Prop
  | declared : DeclCfg d n ⟨p, s⟩ → DeclSettings d s
  | tail : DeclSettings d (pair :: tail) → DeclSettings d tail

/-- Replace the two formerly frozen boundaries with their shipped least-rule
definitions. All other policy/host/name fields remain the original inputs. -/
def model (d : DeclarationInput) : ConfigInput :=
  { d.policy with baseConfig := Base d, declSettings := DeclSettings d }

inductive HasPolicy (d : DeclarationInput) : Symbol → Prop
  | inherit : d.model.settingExecInherit k → HasPolicy d k
  | fixed : d.model.settingExecValue k f → HasPolicy d k

/-- Precisely the validation clauses needed here, not every L1 error. -/
inductive SettingsError (d : DeclarationInput) : Prop
  | reservedValue : d.model.settingValue k d.model.inherit → SettingsError d
  | doublePosition : d.settingKey k i → d.settingKey k j → i < j → SettingsError d
  | missingDefault : d.settingKey k pos → (∀ v, ¬ d.settingDefault k v) → SettingsError d
  | missingPolicy : d.settingKey k pos → ¬ HasPolicy d k → SettingsError d
  | mixedPolicy : d.model.settingExecInherit k → d.model.settingExecValue k f → SettingsError d
  | doublePolicy : d.model.settingExecValue k a → d.model.settingExecValue k b →
      a < b → SettingsError d
  | defaultOutside : d.settingDefault k v → ¬ d.model.settingValue k v → SettingsError d
  | unknownOverride : d.model.execSetting k f →
      (∀ pos, ¬ d.settingKey k pos) → SettingsError d
  | outsideOverride : d.model.execSetting k f → d.settingKey k pos →
      f ≠ d.model.inherit → ¬ d.model.settingValue k f → SettingsError d
  | doubleOverride : d.model.execSetting k a → d.model.execSetting k b →
      a < b → SettingsError d

theorem bad_reports_error (d : DeclarationInput) {k : Symbol}
    (bad : ConfigInput.Bad d.model k) : SettingsError d := by
  cases bad with
  | mixed inherit fixed _ => exact SettingsError.mixedPolicy inherit fixed
  | defaults first second lt _ => exact SettingsError.doublePolicy first second lt
  | overrides first second lt => exact SettingsError.doubleOverride first second lt

theorem exec_value_exists (d : DeclarationInput) (clean : ¬ SettingsError d)
    {k v : Symbol} {pos : Int} (key : d.settingKey k pos)
    (domain : d.model.settingValue k v) : ∃ x, ConfigInput.Value d.model k v x := by
  classical
  have good : ¬ ConfigInput.Bad d.model k := fun bad => clean (d.bad_reports_error bad)
  by_cases overridden : ConfigInput.Overridden d.model k
  · cases overridden with
    | stated policy =>
      rename_i chosen
      by_cases inherit : chosen = d.model.inherit
      · subst_vars
        exact ⟨v, ConfigInput.Value.inheritOverride policy good domain⟩
      · have fixedDomain : d.model.settingValue k chosen := by
          apply Classical.byContradiction
          intro invalid
          exact clean (SettingsError.outsideOverride policy key inherit invalid)
        exact ⟨_, ConfigInput.Value.fixedOverride policy inherit good fixedDomain domain⟩
  · have stated : HasPolicy d k := by
      apply Classical.byContradiction
      intro missing
      exact clean (SettingsError.missingPolicy key missing)
    cases stated with
    | inherit policy =>
      exact ⟨v, ConfigInput.Value.inheritDefault policy overridden good domain⟩
    | fixed policy =>
      exact ⟨_, ConfigInput.Value.fixedDefault policy overridden good domain⟩

theorem decl_value_in_domain (d : DeclarationInput) (clean : ¬ SettingsError d)
    {n k v : Symbol} (value : DeclValue d n k v) : d.model.settingValue k v := by
  classical
  cases value with
  | explicit _ domain => exact domain
  | default _ stated _ =>
    apply Classical.byContradiction
    intro invalid
    exact clean (SettingsError.defaultOutside stated invalid)

theorem error_free_reserved_absent (d : DeclarationInput) (clean : ¬ SettingsError d)
    (k : Symbol) : ¬ d.model.settingValue k d.model.inherit :=
  fun domain => clean (SettingsError.reservedValue domain)

theorem key_priority_unique (d : DeclarationInput) (clean : ¬ SettingsError d)
    {k : Symbol} {i j : Int} (first : d.settingKey k i) (second : d.settingKey k j) :
    i = j := by
  by_cases same : i = j
  · exact same
  have ordered : i < j ∨ j < i := by omega
  rcases ordered with less | greater
  · exact False.elim (clean (SettingsError.doublePosition first second less))
  · exact False.elim (clean (SettingsError.doublePosition second first greater))

theorem rank_declared (d : DeclarationInput) {k : Symbol} {rank : Int}
    (ranked : Rank d k rank) : ∃ priority, d.settingKey k priority := by
  cases ranked with
  | counted key => exact ⟨_, key⟩

theorem suffix_entries (d : DeclarationInput) (clean : ¬ SettingsError d)
    {n : Symbol} {pos : Int} {s : Settings} (suffix : Suffix d n pos s) :
    ∀ k v, (k, v) ∈ s → ∃ index, d.settingKey k index ∧ d.model.settingValue k v := by
  induction suffix with
  | nil _ _ => intro k v member; cases member
  | cons positioned _ value ih =>
    intro k v member
    rcases List.mem_cons.mp member with eq | member
    · cases eq
      cases positioned with
      | minimum ranked _ =>
        obtain ⟨priority, key⟩ := d.rank_declared ranked
        cases value with
        | minimum row _ => exact ⟨_, key, d.decl_value_in_domain clean row⟩
    · exact ih k v member

theorem base_origin (d : DeclarationInput) {c : Config} (base : Base d c) :
    ∃ n p, DeclCfg d n ⟨p, c.settings⟩ := by
  cases base with
  | concrete declared _ => exact ⟨_, _, declared⟩
  | sliced slice =>
    cases slice with
    | platform multi _ =>
      cases multi with
      | declared declared _ => exact ⟨_, _, declared⟩

theorem base_settings_declared (d : DeclarationInput) {c : Config} (base : Base d c) :
    DeclSettings d c.settings := by
  obtain ⟨_, _, declared⟩ := d.base_origin base
  exact DeclSettings.declared declared

theorem base_entries (d : DeclarationInput) (clean : ¬ SettingsError d)
    {c : Config} (base : Base d c) :
    ∀ k v, (k, v) ∈ c.settings →
      ∃ pos, d.settingKey k pos ∧ d.model.settingValue k v := by
  obtain ⟨_, _, declared⟩ := d.base_origin base
  cases declared with
  | record _ suffix => exact d.suffix_entries clean suffix

theorem exec_settings_total (d : DeclarationInput) (clean : ¬ SettingsError d)
    {s : Settings} (declared : DeclSettings d s)
    (entries : ∀ k v, (k, v) ∈ s →
      ∃ pos, d.settingKey k pos ∧ d.model.settingValue k v) :
    ∃ e, ConfigInput.ExecSettings d.model s e := by
  induction s with
  | nil => exact ⟨[], ConfigInput.ExecSettings.nil⟩
  | cons pair tail ih =>
    obtain ⟨k, v⟩ := pair
    obtain ⟨pos, key, domain⟩ := entries k v (List.mem_cons_self)
    obtain ⟨x, minimum⟩ := d.model.minimum_exists (d.exec_value_exists clean key domain)
    obtain ⟨e, mapped⟩ := ih (DeclSettings.tail declared)
      (fun k v member => entries k v (List.mem_cons_of_mem _ member))
    exact ⟨(k, x) :: e, ConfigInput.ExecSettings.cons declared mapped minimum⟩

theorem base_exec_total (d : DeclarationInput) (clean : ¬ SettingsError d)
    {c : Config} (base : Base d c) {host : Symbol} (present : d.model.hostPlatform host) :
    ∃ e, ConfigInput.Exec d.model c e := by
  obtain ⟨e, mapped⟩ := d.exec_settings_total clean (d.base_settings_declared base)
    (d.base_entries clean base)
  cases c
  exact ⟨_, ConfigInput.Exec.base base present mapped⟩

theorem base_exec_exists_unique (d : DeclarationInput) (settingsClean : ¬ SettingsError d)
    (execClean : ¬ ConfigInput.Diagnostic d.model)
    (uniqueHost : ∀ a b, d.model.hostPlatform a → d.model.hostPlatform b → a = b)
    {c : Config} (base : Base d c) {host : Symbol} (present : d.model.hostPlatform host) :
    ∃ e, ConfigInput.Exec d.model c e ∧
      ∀ other, ConfigInput.Exec d.model c other → other = e := by
  obtain ⟨e, mapped⟩ := d.base_exec_total settingsClean base present
  exact ⟨e, mapped, fun _ other =>
    d.model.exec_config_functional_of_host execClean uniqueHost other mapped⟩

end DeclarationInput

structure ExecutionContext where
  declarations : DeclarationInput
  settingsClean : ¬ DeclarationInput.SettingsError declarations
  execClean : ¬ ConfigInput.Diagnostic declarations.model
  host : Symbol
  hostPresent : declarations.model.hostPlatform host
  uniqueHost : ∀ a b, declarations.model.hostPlatform a →
    declarations.model.hostPlatform b → a = b

namespace ExecutionContext

abbrev input (ctx : ExecutionContext) : ConfigInput := ctx.declarations.model
abbrev Configuration (ctx : ExecutionContext) := {c : Config // ConfigInput.Known ctx.input c}

theorem known_exec_total (ctx : ExecutionContext) {c : Config}
    (known : ConfigInput.Known ctx.input c) : ∃ e, ConfigInput.Exec ctx.input c e := by
  cases known with
  | base base => exact ctx.declarations.base_exec_total ctx.settingsClean base ctx.hostPresent
  | exec image => exact ⟨_, ConfigInput.Exec.reflected image⟩

noncomputable def tool (ctx : ExecutionContext) (c : ctx.Configuration) : ctx.Configuration :=
  ⟨Classical.choose (ctx.known_exec_total c.property),
    ConfigInput.Known.exec (Classical.choose_spec (ctx.known_exec_total c.property))⟩

theorem tool_spec (ctx : ExecutionContext) (c : ctx.Configuration) :
    ConfigInput.Exec ctx.input c.val (ctx.tool c).val :=
  Classical.choose_spec (ctx.known_exec_total c.property)

theorem tool_unique (ctx : ExecutionContext) (c : ctx.Configuration) {e : Config}
    (exec : ConfigInput.Exec ctx.input c.val e) : e = (ctx.tool c).val :=
  ctx.input.exec_config_functional_of_host ctx.execClean ctx.uniqueHost exec (ctx.tool_spec c)

theorem tool_host (ctx : ExecutionContext) (c : ctx.Configuration) :
    (ctx.tool c).val.platform = ctx.host :=
  ctx.input.exec_targets_the_host ctx.uniqueHost ctx.hostPresent (ctx.tool_spec c)

theorem tool_idempotent (ctx : ExecutionContext) (c : ctx.Configuration) :
    ctx.tool (ctx.tool c) = ctx.tool c := by
  apply Subtype.ext
  exact (ctx.tool_unique (ctx.tool c) (ConfigInput.Exec.reflected (ctx.tool_spec c))).symm

end ExecutionContext
end Buildlog.Core
