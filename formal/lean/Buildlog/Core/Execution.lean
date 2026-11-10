-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Core.Configuration

namespace Buildlog.Core.ConfigInput

theorem value_input_in_domain (i : ConfigInput) {k v x : Symbol}
    (value : Value i k v x) : i.settingValue k v := by
  cases value <;> assumption

/-- The ordinal minimum exists for every nonempty group. No functionality of
the raw exec_value relation, nor additional finite-base axiom, is needed. -/
theorem minimum_exists (i : ConfigInput) {k v : Symbol}
    (nonempty : ∃ x, Value i k v x) : ∃ x, i.Minimum k v x := by
  classical
  have bounded : ∀ n, (∃ x, Value i k v x ∧ i.ordinal x = n) →
      ∃ x, i.Minimum k v x := by
    intro n
    induction n using Nat.strongRecOn with
    | ind n ih =>
      rintro ⟨x, hx, eq⟩
      by_cases smaller : ∃ y, Value i k v y ∧ i.ordinal y < n
      · obtain ⟨y, hy, lt⟩ := smaller
        exact ih (i.ordinal y) lt ⟨y, hy, rfl⟩
      · refine ⟨x, hx, ?_⟩
        intro y hy
        rw [eq]
        exact Nat.le_of_not_gt (fun lt => smaller ⟨y, hy, lt⟩)
  obtain ⟨x, hx⟩ := nonempty
  exact bounded (i.ordinal x) ⟨x, hx, rfl⟩

/-- The actual not-idempotent check supplies a self-row. Fixed-policy rows
transfer back to the original input; inherited rows are already self-rows.
Reserved setting-domain values are rejected separately by SettingsError. -/
theorem minimum_idempotent (i : ConfigInput) (clean : ¬ Diagnostic i)
    {k v x : Symbol} (minimum : i.Minimum k v x) : i.Minimum k x x := by
  classical
  have self : Value i k x x := by
    apply Classical.byContradiction
    intro missing
    exact clean (Diagnostic.notIdempotent minimum.1 missing)
  refine ⟨self, ?_⟩
  intro y value
  cases value with
  | inheritDefault _ _ _ _ => exact Nat.le_refl _
  | inheritOverride _ _ _ => exact Nat.le_refl _
  | fixedDefault policy overridden bad _ =>
    exact minimum.2 y (Value.fixedDefault policy overridden bad
      (i.value_input_in_domain minimum.1))
  | fixedOverride policy notInherit bad domain _ =>
    exact minimum.2 y (Value.fixedOverride policy notInherit bad domain
      (i.value_input_in_domain minimum.1))

theorem exec_settings_image_fixed (i : ConfigInput) (clean : ¬ Diagnostic i)
    {s e f : Settings} (first : ExecSettings i s e) (next : ExecSettings i e f) :
    f = e := by
  induction first generalizing f with
  | nil => cases next; rfl
  | cons _ _ minimum ih =>
    cases next with
    | cons _ tail mapped =>
      have head := i.minimum_unique mapped (i.minimum_idempotent clean minimum)
      have rest := ih tail
      simp only [head, rest]

theorem exec_image_settings_fixed (i : ConfigInput) (clean : ¬ Diagnostic i)
    {c e : Config} {f : Settings} (image : Exec i c e)
    (mapped : ExecSettings i e.settings f) : f = e.settings := by
  induction image with
  | base _ _ settings => exact i.exec_settings_image_fixed clean settings mapped
  | reflected _ ih => exact ih mapped

theorem exec_image_fixed (i : ConfigInput) (clean : ¬ Diagnostic i)
    (uniqueHost : ∀ a b, i.hostPlatform a → i.hostPlatform b → a = b)
    {c e f : Config} (image : Exec i c e) (next : Exec i e f) : f = e := by
  cases next with
  | base _ host settings =>
    have platform := uniqueHost _ _ host (i.exec_targets_host image)
    have mapped := i.exec_image_settings_fixed clean image settings
    simp only [platform, mapped]
  | reflected _ => rfl

/-- Unlike the earlier naming-based theorem, this uses host uniqueness and
the policy/minimum structure. No cfg_name existence premise is required. -/
theorem exec_config_functional_of_host (i : ConfigInput) (clean : ¬ Diagnostic i)
    (uniqueHost : ∀ a b, i.hostPlatform a → i.hostPlatform b → a = b)
    {c a b : Config} (first : Exec i c a) (second : Exec i c b) : a = b := by
  cases first with
  | base base host settings =>
    cases second with
    | base _ host' settings' =>
      have platform := uniqueHost _ _ host host'
      have mapped := i.exec_settings_functional settings settings'
      simp only [platform, mapped]
    | reflected image =>
      exact i.exec_image_fixed clean uniqueHost image (Exec.base base host settings)
  | reflected image =>
    exact (i.exec_image_fixed clean uniqueHost image second).symm

theorem inherit_override_preserves_value (i : ConfigInput) {k v x : Symbol}
    (policy : i.execSetting k i.inherit) (value : Value i k v x) : x = v := by
  cases value with
  | inheritDefault _ notOverridden _ _ =>
    exact False.elim (notOverridden (Overridden.stated policy))
  | fixedDefault _ notOverridden _ _ =>
    exact False.elim (notOverridden (Overridden.stated policy))
  | inheritOverride _ _ _ => rfl
  | fixedOverride other notInherit good _ _ =>
    rcases Nat.lt_or_gt_of_ne notInherit with less | greater
    · exact False.elim (good (Bad.overrides other policy less))
    · exact False.elim (good (Bad.overrides policy other greater))

end Buildlog.Core.ConfigInput
