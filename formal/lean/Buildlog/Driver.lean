-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Semantics

namespace Buildlog
open Facts

/-- Functional snapshot oracle β. Domains/tuple checks happen before probing;
shape/declaration/primary checks happen after collecting the whole batch.
`total_if_checked` is the P1 check's successful branch, not a probe axiom. -/
structure Driver (n a m : Nat) where
  model : Facts a → Facts m
  requests : Facts m → Facts n
  guarded : Facts n
  primary : Fin n → Fin a → Prop
  beta : Fin n → Facts a → Facts a
  badBefore : Facts a → Prop
  badAfter : Facts a → Facts n → Facts a → Prop
  total_if_checked : ∀ e batch rows,
    rows = (fun r => ∃ i, batch i ∧ beta i e r) →
    ¬ badAfter e batch rows →
    ∀ i, batch i → ∃ r, primary i r ∧ rows r

namespace Driver

def answered (p : Driver n a m) (e : Facts a) : Facts n :=
  fun i => ∃ r, p.primary i r ∧ e r

def pending (p : Driver n a m) (e : Facts a) : Facts n :=
  diff (p.requests (p.model e)) (p.answered e)

noncomputable def batch (p : Driver n a m) (e : Facts a) : Facts n := by
  classical
  exact if ∃ i, p.pending e i ∧ p.guarded i then
    (fun i => p.pending e i ∧ p.guarded i) else p.pending e

def produced (p : Driver n a m) (e : Facts a) : Facts a :=
  fun r => ∃ i, p.batch e i ∧ p.beta i e r

def Halt (p : Driver n a m) (e : Facts a) : Prop :=
  p.badBefore e ∨ (∀ i, ¬ p.pending e i) ∨ p.badAfter e (p.batch e) (p.produced e)

noncomputable def step (p : Driver n a m) (e : Facts a) : Facts a := by
  classical
  exact if p.Halt e then e else union e (p.produced e)

noncomputable def rounds (p : Driver n a m) (k : Nat) : Facts a :=
  iterate p.step k empty

theorem batch_sub_pending (p : Driver n a m) (e : Facts a) :
    Sub (p.batch e) (p.pending e) := by
  classical
  unfold batch
  split
  · exact fun _ hi => hi.1
  · exact sub_refl _

theorem guarded_first (p : Driver n a m) (e : Facts a)
    (h : ∃ i, p.pending e i ∧ p.guarded i) : Sub (p.batch e) p.guarded := by
  classical
  simp only [batch, h, ↓reduceIte]
  exact fun _ hi => hi.2

theorem batch_nonempty (p : Driver n a m) (e : Facts a)
    (h : ∃ i, p.pending e i) : ∃ i, p.batch e i := by
  classical
  unfold batch
  split
  · assumption
  · exact h

theorem answered_grows (p : Driver n a m) (e : Facts a) :
    Sub (p.answered e) (p.answered (p.step e)) := by
  classical
  intro i ⟨r, hr, he⟩
  refine ⟨r, hr, ?_⟩
  unfold step
  split
  · exact he
  · exact Or.inl he

theorem progress (p : Driver n a m) (e : Facts a) (h : ¬ p.Halt e) :
    card (p.answered e) < card (p.answered (p.step e)) := by
  classical
  have pending : ∃ i, p.pending e i := by
    apply Classical.byContradiction
    intro hn
    exact h (Or.inr (Or.inl (fun i hi => hn ⟨i, hi⟩)))
  obtain ⟨i, hi⟩ := p.batch_nonempty e pending
  have valid : ¬ p.badAfter e (p.batch e) (p.produced e) :=
    fun hb => h (Or.inr (Or.inr hb))
  obtain ⟨r, hr, hrow⟩ := p.total_if_checked e _ _ rfl valid i hi
  apply card_strict (p.answered_grows e)
  refine ⟨i, ⟨r, hr, ?_⟩, (p.batch_sub_pending e i hi).2⟩
  simp only [step, h, ↓reduceIte, union]
  exact Or.inr hrow

theorem halt_iff_fixed (p : Driver n a m) (e : Facts a) :
    p.Halt e ↔ p.step e = e := by
  classical
  constructor
  · intro h; simp only [step, h, ↓reduceIte]
  · intro eq
    apply Classical.byContradiction
    intro hn
    have strict := p.progress e hn
    rw [eq] at strict
    omega

/-- Includes the last model/check observation: `k + 1 ≤ n + 1`.
`n` counts distinct (kind,id) pairs, hence is at most the paper's sum of tuple
universe sizes. Check failures are halting states, not successful closures. -/
theorem fresh_termination (p : Driver n a m) :
    ∃ k, k ≤ n ∧ p.Halt (p.rounds k) := by
  have prog : ∀ e, p.step e ≠ e → card (p.answered e) < card (p.answered (p.step e)) := by
    intro e h
    exact p.progress e (fun hh => h ((p.halt_iff_fixed e).mp hh))
  obtain ⟨k, hk, eq⟩ := bounded_progress p.step (fun e => card (p.answered e))
    (fun e => card_bound _) prog empty
  refine ⟨k, by omega, (p.halt_iff_fixed _).mpr ?_⟩
  exact (iterate_step p.step k empty).symm.trans eq

/-- A set-fold characterization makes enumeration order and duplicates inert.
All β calls read `snapshot`, never the accumulated rows. -/
def collect (p : Driver n a m) (snapshot : Facts a) : List (Fin n) → Facts a
  | [] => empty
  | i :: rest => union (p.beta i snapshot) (p.collect snapshot rest)

theorem collect_spec (p : Driver n a m) (snapshot : Facts a) (xs : List (Fin n)) (r : Fin a) :
    p.collect snapshot xs r ↔ ∃ i, i ∈ xs ∧ p.beta i snapshot r := by
  induction xs with
  | nil => simp [collect, empty]
  | cons i xs ih =>
    simp only [collect, union, ih, List.mem_cons]
    grind

theorem probe_order_independent (p : Driver n a m) (e : Facts a)
    (xs ys : List (Fin n)) (h : ∀ i, i ∈ xs ↔ i ∈ ys) :
    p.collect e xs = p.collect e ys := by
  apply Facts.ext
  intro r
  simp only [p.collect_spec]
  constructor
  · rintro ⟨i, hi, hr⟩; exact ⟨i, (h i).mp hi, hr⟩
  · rintro ⟨i, hi, hr⟩; exact ⟨i, (h i).mpr hi, hr⟩

/-- Any implementation trace satisfying the functional round equation is the
same trace. Set-valued input ingestion is independent of file/fact order. -/
theorem fresh_determinism (p : Driver n a m) (trace : Nat → Facts a)
    (zero : trace 0 = empty) (next : ∀ k, trace (k + 1) = p.step (trace k)) :
    ∀ k, trace k = p.rounds k := by
  intro k
  induction k with
  | zero => exact zero
  | succ k ih =>
    rw [next, ih]
    exact (iterate_step p.step k empty).symm

theorem stopping_round_deterministic (p : Driver n a m)
    {i j : Nat} (hi : p.Halt (p.rounds i)) (hj : p.Halt (p.rounds j))
    (first_i : ∀ k, k < i → ¬ p.Halt (p.rounds k))
    (first_j : ∀ k, k < j → ¬ p.Halt (p.rounds k)) : i = j := by
  by_cases hij : i < j
  · exact False.elim (first_j i hij hi)
  · by_cases hji : j < i
    · exact False.elim (first_i j hji hj)
    · omega

theorem first_stopping_round (p : Driver n a m) :
    ∃ k, k ≤ n ∧ p.Halt (p.rounds k) ∧
      ∀ j, j < k → ¬ p.Halt (p.rounds j) := by
  classical
  have first : ∀ b, (∃ k, k ≤ b ∧ p.Halt (p.rounds k)) →
      ∃ k, k ≤ b ∧ p.Halt (p.rounds k) ∧
        ∀ j, j < k → ¬ p.Halt (p.rounds j) := by
    intro b
    induction b with
    | zero =>
      rintro ⟨k, hk, halt⟩
      have eq : k = 0 := by omega
      subst k
      exact ⟨0, hk, halt, by intro j hj; omega⟩
    | succ b ih =>
      intro hex
      by_cases prev : ∃ k, k ≤ b ∧ p.Halt (p.rounds k)
      · obtain ⟨k, hk, hh, hf⟩ := ih prev
        exact ⟨k, by omega, hh, hf⟩
      · obtain ⟨k, hk, halt⟩ := hex
        have eq : k = b + 1 := by
          by_cases h : k ≤ b
          · exact False.elim (prev ⟨k, h, halt⟩)
          · omega
        refine ⟨k, hk, halt, ?_⟩
        intro j hj hh
        exact prev ⟨j, by omega, hh⟩
  exact first n p.fresh_termination

end Driver
end Buildlog
