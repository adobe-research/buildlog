-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Finite

namespace Buildlog
open Facts

/-- Positive recursion within a stratum. Lower-stratum negation and aggregates
are parameters; they need not be monotone in those parameters. -/
structure Positive (m : Nat) where
  step : Facts m → Facts m
  grows : ∀ s, Sub s (step s)
  mono : ∀ {s t}, Sub s t → Sub (step s) (step t)

namespace Positive

noncomputable def stop (p : Positive m) (base : Facts m) : Nat :=
  Classical.choose (finite_progress p.step p.grows base)

theorem stop_spec (p : Positive m) (base : Facts m) :
    stop p base + card base ≤ m ∧
      orbit p.step base (stop p base + 1) = orbit p.step base (stop p base) :=
  Classical.choose_spec (finite_progress p.step p.grows base)

noncomputable def model (p : Positive m) (base : Facts m) : Facts m :=
  orbit p.step base (stop p base)

theorem fixed (p : Positive m) (base : Facts m) : p.step (p.model base) = p.model base :=
  (stop_spec p base).2

theorem base_sub_iterate (p : Positive m) (base : Facts m) (k : Nat) :
    Sub base (orbit p.step base k) := by
  induction k with
  | zero => exact sub_refl _
  | succ k ih => exact sub_trans ih (p.grows _)

theorem iterate_sub_fixed (p : Positive m) {base t : Facts m}
    (hb : Sub base t) (hf : p.step t = t) (k : Nat) :
    Sub (orbit p.step base k) t := by
  induction k with
  | zero => exact hb
  | succ k ih => simpa only [orbit, hf] using p.mono ih

theorem iterate_sub_model (p : Positive m) (base : Facts m) (k : Nat) :
    Sub (orbit p.step base k) (p.model base) :=
  iterate_sub_fixed p (base_sub_iterate p base _) (fixed p base) k

theorem least (p : Positive m) {base t : Facts m}
    (hb : Sub base t) (hf : p.step t = t) : Sub (p.model base) t :=
  iterate_sub_fixed p hb hf _

/-- Agreement only along one least-model computation suffices. -/
theorem model_eq_of_agree (p q : Positive m) (base : Facts m)
    (agree : ∀ j, Sub j (p.model base) → p.step j = q.step j) :
    p.model base = q.model base := by
  have iter : ∀ k, orbit p.step base k = orbit q.step base k := by
    intro k
    induction k with
    | zero => rfl
    | succ k ih =>
      change p.step (orbit p.step base k) = q.step (orbit q.step base k)
      rw [agree _ (iterate_sub_model p base k), ih]
  apply sub_antisymm
  · rw [model, iter]
    exact iterate_sub_model q base _
  · apply least q (base_sub_iterate p base _)
    change q.step (p.model base) = p.model base
    rw [← agree _ (sub_refl _)]
    exact fixed p base

theorem iterate_after_stop (p : Positive m) (base : Facts m) {k : Nat}
    (hk : p.stop base ≤ k) : orbit p.step base k = p.model base := by
  have aux : ∀ j, orbit p.step base (p.stop base + j) = p.model base := by
    intro j
    induction j with
    | zero => rfl
    | succ j ih =>
      change p.step (orbit p.step base (p.stop base + j)) = p.model base
      rw [ih, p.fixed]
  have eq : k = p.stop base + (k - p.stop base) := by omega
  rw [eq]
  exact aux _

/-- Dependency slicing is a local commutation property, not a presumed
equality of final models. Both computations are run to a common iteration. -/
theorem slice_model_eq (p q : Positive m) (base₁ base₂ : Facts m)
    (view : Facts m → Facts d) (base_eq : view base₁ = view base₂)
    (local_eq : ∀ s t, view s = view t → view (p.step s) = view (q.step t)) :
    view (p.model base₁) = view (q.model base₂) := by
  have iter : ∀ k, view (orbit p.step base₁ k) = view (orbit q.step base₂ k) := by
    intro k
    induction k with
    | zero => exact base_eq
    | succ k ih => exact local_eq _ _ ih
  have eq := iter (p.stop base₁ + q.stop base₂)
  rw [p.iterate_after_stop base₁ (by omega), q.iterate_after_stop base₂ (by omega)] at eq
  exact eq

end Positive

/-- Fixed author/user inputs and the composed program are captured by `stratum`.
`n` indexes stable (kind, identifier, arguments) answer bundles; `m` indexes
non-result ground facts. Results are separate, so model equality is paper `≈`.
Each stage starts with the complete lower model. -/
structure Stratified (n m : Nat) where
  height : Nat
  stratum : Fin height → Facts n → Facts m → Positive m
  requests : Facts m → Facts n
  requests_mono : ∀ {s t}, Sub s t → Sub (requests s) (requests t)
  guarded : Facts n
  /-- A semantic, local operator contract for W3 + RL. Changed bundles can
  affect a rule instance only when their positive request guard holds in the
  current interpretation. This applies also to negative/aggregate reads. -/
  guarded_local : ∀ s e x lower j,
    Sub x guarded → (∀ i, x i → ¬ requests j i) →
    (stratum s e lower).step j = (stratum s (union e x) lower).step j

namespace Stratified

noncomputable def stages (p : Stratified n m) (e : Facts n) : (k : Nat) →
    k ≤ p.height → Facts m
  | 0, _ => empty
  | k + 1, h => (p.stratum ⟨k, by omega⟩ e (stages p e k (by omega))).model
      (stages p e k (by omega))

noncomputable def model (p : Stratified n m) (e : Facts n) : Facts m :=
  stages p e p.height (Nat.le_refl _)

theorem stages_sub (p : Stratified n m) (e : Facts n)
    {k l : Nat} (hk : k ≤ p.height) (hl : l ≤ p.height) (hkl : k ≤ l) :
    Sub (p.stages e k hk) (p.stages e l hl) := by
  induction l with
  | zero =>
    have : k = 0 := by omega
    subst k
    exact sub_refl _
  | succ l ih =>
    by_cases heq : k = l + 1
    · subst k; exact sub_refl _
    · exact sub_trans (ih (by omega) (by omega))
        (Positive.base_sub_iterate _ _ _)

theorem finite_rounds (p : Stratified n m) (e : Facts n) :
    card e ≤ n ∧ card (p.model e) ≤ m :=
  ⟨card_bound e, card_bound _⟩

/-- Paper inert answers, first orientation: requests absent before addition. -/
theorem inert_before (p : Stratified n m) (e x : Facts n)
    (gx : Sub x p.guarded) (absent : ∀ i, x i → ¬ p.requests (p.model e) i) :
    p.model e = p.model (union e x) := by
  have stages_eq : ∀ k (h : k ≤ p.height),
      p.stages e k h = p.stages (union e x) k h := by
    intro k
    induction k with
    | zero => intro h; rfl
    | succ k ih =>
      intro h
      have hlow := ih (by omega)
      simp only [stages, ← hlow]
      apply Positive.model_eq_of_agree
      intro j hj
      apply p.guarded_local _ e x _ j gx
      intro i hxi hri
      apply absent i hxi
      apply p.requests_mono
        (sub_trans hj (p.stages_sub e (k := k + 1) (by omega)
          (Nat.le_refl _) (by omega)))
      exact hri
  exact stages_eq _ _

/-- Paper inert answers, including absence in the model after addition. -/
theorem inert_answers (p : Stratified n m) (e x : Facts n)
    (gx : Sub x p.guarded)
    (absent : (∀ i, x i → ¬ p.requests (p.model e) i) ∨
      (∀ i, x i → ¬ p.requests (p.model (union e x)) i)) :
    p.model e = p.model (union e x) := by
  rcases absent with before | after
  · exact p.inert_before e x gx before
  · have stages_eq : ∀ k (h : k ≤ p.height),
        p.stages (union e x) k h = p.stages e k h := by
      intro k
      induction k with
      | zero => intro h; rfl
      | succ k ih =>
        intro h
        have hlow := ih (by omega)
        simp only [stages, ← hlow]
        apply Positive.model_eq_of_agree
        intro j hj
        symm
        apply p.guarded_local _ e x _ j gx
        intro i hxi hri
        apply after i hxi
        apply p.requests_mono
          (sub_trans hj (p.stages_sub (union e x) (k := k + 1) (by omega)
            (Nat.le_refl _) (by omega)))
        exact hri
    exact (stages_eq _ _).symm

end Stratified
end Buildlog
