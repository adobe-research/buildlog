-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Std

namespace Buildlog.Core

/-- Injective symbol ranks. Only equality and the shipped diagnostic/minimum
ordering are used; no proof depends on the spelling of an arbitrary symbol. -/
abbrev Symbol := Nat
abbrev Settings := List (Symbol × Symbol)

structure Config where
  platform : Symbol
  settings : Settings
  deriving DecidableEq

/-- Completed declaration/policy/name relations supplied by lower strata.
`inherit` denotes the literal symbol "inherit", not an extra Datalog sort. -/
structure ConfigInput where
  inherit : Symbol
  ordinal : Symbol → Nat
  ordinal_injective : ∀ a b, ordinal a = ordinal b → a = b
  settingValue : Symbol → Symbol → Prop
  settingExecInherit : Symbol → Prop
  settingExecValue : Symbol → Symbol → Prop
  execSetting : Symbol → Symbol → Prop
  declSettings : Settings → Prop
  baseConfig : Config → Prop
  hostPlatform : Symbol → Prop
  cfgName : Config → Symbol → Prop

namespace ConfigInput

-- L1_targets.dl: exec_overridden, exec_bad, exec_value.
inductive Overridden (i : ConfigInput) : Symbol → Prop
  | stated : i.execSetting k p → Overridden i k

inductive Bad (i : ConfigInput) : Symbol → Prop
  | mixed : i.settingExecInherit k → i.settingExecValue k f →
      ¬ Overridden i k → Bad i k
  | defaults : i.settingExecValue k a → i.settingExecValue k b →
      a < b → ¬ Overridden i k → Bad i k
  | overrides : i.execSetting k a → i.execSetting k b → a < b → Bad i k

inductive Value (i : ConfigInput) : Symbol → Symbol → Symbol → Prop
  | inheritDefault : i.settingExecInherit k → ¬ Overridden i k →
      ¬ Bad i k → i.settingValue k v → Value i k v v
  | fixedDefault : i.settingExecValue k f → ¬ Overridden i k →
      ¬ Bad i k → i.settingValue k v → Value i k v f
  | inheritOverride : i.execSetting k i.inherit → ¬ Bad i k →
      i.settingValue k v → Value i k v v
  | fixedOverride : i.execSetting k f → f ≠ i.inherit → ¬ Bad i k →
      i.settingValue k f → i.settingValue k v → Value i k v f

/-- The aggregate `mx = min ord(x0) : { exec_value(k,v,x0) }`,
joined to `exec_value(k,v,x), ord(x)=mx`. This does not presume that
exec_value is functional, or that a minimum exists for an empty group. -/
def Minimum (i : ConfigInput) (k v x : Symbol) : Prop :=
  Value i k v x ∧ ∀ y, Value i k v y → i.ordinal x ≤ i.ordinal y

-- L1_targets.dl: the fact and recursive rule for exec_settings.
inductive ExecSettings (i : ConfigInput) : Settings → Settings → Prop
  | nil : ExecSettings i [] []
  | cons : i.declSettings ((k, v) :: tail) → ExecSettings i tail rest →
      i.Minimum k v x → ExecSettings i ((k, v) :: tail) ((k, x) :: rest)

theorem minimum_unique (i : ConfigInput) {k v x y : Symbol}
    (hx : i.Minimum k v x) (hy : i.Minimum k v y) : x = y :=
  i.ordinal_injective x y (Nat.le_antisymm (hx.2 y hy.1) (hy.2 x hx.1))

theorem exec_settings_functional (i : ConfigInput) {s a b : Settings}
    (ha : ExecSettings i s a) (hb : ExecSettings i s b) : a = b := by
  induction ha generalizing b with
  | nil => cases hb; rfl
  | cons declared tail minimum ih =>
    cases hb with
    | cons _ other minimum' =>
      have head := i.minimum_unique minimum minimum'
      have rest := ih other
      simp only [head, rest]

theorem exec_settings_preserves_keys (i : ConfigInput) {s e : Settings}
    (h : ExecSettings i s e) : e.map Prod.fst = s.map Prod.fst := by
  induction h with
  | nil => rfl
  | cons _ _ _ ih => simp only [List.map_cons, ih]

theorem exec_settings_preserves_length (i : ConfigInput) {s e : Settings}
    (h : ExecSettings i s e) : e.length = s.length := by
  induction h with
  | nil => rfl
  | cons _ _ _ ih => simp only [List.length_cons, ih]

-- L1_targets.dl: exec_config's two clauses, including positive recursion.
inductive Exec (i : ConfigInput) : Config → Config → Prop
  | base : i.baseConfig ⟨p, s⟩ → i.hostPlatform h → ExecSettings i s e →
      Exec i ⟨p, s⟩ ⟨h, e⟩
  | reflected : Exec i c e → Exec i e e

/-- The declaration/slice portion of `config` is frozen as `baseConfig`;
the only exec-generated addition is `config(e) :- exec_config(_,e)`. -/
inductive Known (i : ConfigInput) : Config → Prop
  | base : i.baseConfig c → Known i c
  | exec : Exec i c e → Known i e

inductive Platform (i : ConfigInput) : Config → Symbol → Prop
  | config : Known i ⟨p, s⟩ → Platform i ⟨p, s⟩ p

theorem platform_eq (i : ConfigInput) {c : Config} {p : Symbol}
    (h : Platform i c p) : p = c.platform := by
  cases h; rfl

theorem exec_source_known (i : ConfigInput) {c e : Config} (h : Exec i c e) :
    Known i c := by
  cases h with
  | base hb _ _ => exact Known.base hb
  | reflected prior => exact Known.exec prior

theorem exec_platform_known (i : ConfigInput) {c e : Config} (h : Exec i c e) :
    Platform i e e.platform := by
  cases e
  exact Platform.config (Known.exec h)

theorem known_platform (i : ConfigInput) {c : Config} (h : Known i c) :
    Platform i c c.platform := by
  cases c
  exact Platform.config h

/-- Induction on the least derivation includes reflected exec configurations:
every destination, not only a direct base destination, has a host platform. -/
theorem exec_targets_host (i : ConfigInput) {c e : Config} (h : Exec i c e) :
    i.hostPlatform e.platform := by
  induction h with
  | base _ host _ => exact host
  | reflected _ ih => exact ih

theorem exec_targets_the_host (i : ConfigInput) {c e : Config} {host : Symbol}
    (uniqueHost : ∀ a b, i.hostPlatform a → i.hostPlatform b → a = b)
    (hostPresent : i.hostPlatform host) (h : Exec i c e) : e.platform = host :=
  uniqueHost _ _ (i.exec_targets_host h) hostPresent

theorem exec_image_reflexive (i : ConfigInput) {c e : Config} (h : Exec i c e) :
    Exec i e e := Exec.reflected h

-- The shipped naming/idempotence checks, with diagnostic text erased.
inductive Diagnostic (i : ConfigInput) : Prop
  | sharedName : i.cfgName a n → i.cfgName b n → a ≠ b → Diagnostic i
  | notFunctional : Exec i c a → Exec i c b → i.cfgName c n →
      i.cfgName a na → i.cfgName b nb → na < nb → Diagnostic i
  | notIdempotent : Value i k v x → ¬ Value i k x x → Diagnostic i

/-- An error-free, named configuration has at most one exec destination.
Name existence is explicit because the actual diagnostic joins cfg_name.
No raw-policy functionality or idempotence assumption is smuggled in. -/
theorem exec_config_functional (i : ConfigInput)
    (clean : ¬ Diagnostic i)
    (named : ∀ c, Known i c → ∃ n, i.cfgName c n)
    {c a b : Config} (ha : Exec i c a) (hb : Exec i c b) : a = b := by
  obtain ⟨n, hn⟩ := named c (i.exec_source_known ha)
  obtain ⟨na, hna⟩ := named a (Known.exec ha)
  obtain ⟨nb, hnb⟩ := named b (Known.exec hb)
  by_cases eq : a = b
  · exact eq
  · have different : na ≠ nb := by
      intro heq
      subst nb
      exact clean (Diagnostic.sharedName hna hnb eq)
    have order : na < nb ∨ nb < na := Nat.lt_or_gt_of_ne different
    rcases order with lt | gt
    · exact False.elim (clean (Diagnostic.notFunctional ha hb hn hna hnb lt))
    · exact False.elim (clean (Diagnostic.notFunctional hb ha hn hnb hna gt))

theorem exec_config_idempotent (i : ConfigInput)
    (clean : ¬ Diagnostic i)
    (named : ∀ c, Known i c → ∃ n, i.cfgName c n)
    {c e f : Config} (h : Exec i c e) (next : Exec i e f) : f = e :=
  i.exec_config_functional clean named next (Exec.reflected h)

/-- The cons rule and the reflexive-image rule both preserve key order. -/
theorem exec_preserves_keys (i : ConfigInput) {c e : Config} (h : Exec i c e) :
    e.settings.map Prod.fst = c.settings.map Prod.fst := by
  cases h with
  | base _ _ settings => exact i.exec_settings_preserves_keys settings
  | reflected _ => rfl

end ConfigInput
end Buildlog.Core
