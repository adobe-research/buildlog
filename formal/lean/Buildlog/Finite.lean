-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Std

namespace Buildlog

/-- A subset of a fixed, indexed finite universe. No decidability assumption is
needed: these proofs are classical, not an executable replacement for Python. -/
abbrev Facts (n : Nat) := Fin n → Prop

namespace Facts

def Sub (s t : Facts n) : Prop := ∀ i, s i → t i
def union (s t : Facts n) : Facts n := fun i => s i ∨ t i
def diff (s t : Facts n) : Facts n := fun i => s i ∧ ¬ t i
def empty : Facts n := fun _ => False
def full : Facts n := fun _ => True

noncomputable def card : {n : Nat} → Facts n → Nat
  | 0, _ => 0
  | n + 1, s => by
    classical
    exact (if s 0 then 1 else 0) + @card n (fun i => s i.succ)

theorem ext {s t : Facts n} (h : ∀ i, s i ↔ t i) : s = t :=
  funext fun i => propext (h i)

theorem sub_refl (s : Facts n) : Sub s s := fun _ h => h
theorem sub_trans {s t u : Facts n} (h : Sub s t) (k : Sub t u) : Sub s u :=
  fun i hi => k i (h i hi)
theorem sub_antisymm {s t : Facts n} (h : Sub s t) (k : Sub t s) : s = t :=
  ext fun i => ⟨h i, k i⟩

theorem card_le {s t : Facts n} (h : Sub s t) : card s ≤ card t := by
  classical
  induction n with
  | zero => simp [card]
  | succ n ih =>
    have ht := ih (s := fun i => s i.succ) (t := fun i => t i.succ)
      (fun i hi => h i.succ hi)
    have hz := h 0
    simp only [card]
    split <;> split <;> simp_all <;> omega

theorem card_bound (s : Facts n) : card s ≤ n := by
  classical
  induction n with
  | zero => simp [card]
  | succ n ih =>
    have ht := ih (fun i => s i.succ)
    simp only [card]
    split <;> omega

theorem card_strict {s t : Facts n} (h : Sub s t)
    (w : ∃ i, t i ∧ ¬ s i) : card s < card t := by
  classical
  induction n with
  | zero => obtain ⟨i, _⟩ := w; exact Fin.elim0 i
  | succ n ih =>
    have ht := card_le (s := fun i => s i.succ) (t := fun i => t i.succ)
      (fun i hi => h i.succ hi)
    obtain ⟨i, hit, his⟩ := w
    cases i using Fin.cases with
    | zero =>
      simp only [card, hit, his, ↓reduceIte]
      omega
    | succ j =>
      have hs := ih (s := fun i => s i.succ) (t := fun i => t i.succ)
        (fun i hi => h i.succ hi) ⟨j, hit, his⟩
      have hz := h 0
      simp only [card]
      split <;> split <;> simp_all <;> omega

def orbit (step : Facts n → Facts n) (s : Facts n) : Nat → Facts n
  | 0 => s
  | k + 1 => step (orbit step s k)

/-- At most `n - card s` strict growth steps, followed by a final observation. -/
theorem finite_progress (step : Facts n → Facts n)
    (grow : ∀ s, Sub s (step s)) (s : Facts n) :
    ∃ k, k + card s ≤ n ∧ orbit step s (k + 1) = orbit step s k := by
  classical
  have aux : ∀ fuel s, n < card s + fuel →
      ∃ k, k < fuel ∧ orbit step s (k + 1) = orbit step s k := by
    intro fuel
    induction fuel with
    | zero =>
      intro s h
      have := card_bound s
      omega
    | succ fuel ih =>
      intro s h
      by_cases eq : step s = s
      · exact ⟨0, by omega, eq⟩
      · have witness : ∃ i, step s i ∧ ¬ s i := by
          apply Classical.byContradiction
          intro hw
          apply eq
          apply sub_antisymm
          · intro i hi
            apply Classical.byContradiction
            intro hn
            exact hw ⟨i, hi, hn⟩
          · exact grow s
        have strict := card_strict (grow s) witness
        obtain ⟨k, hk, heq⟩ := ih (step s) (by omega)
        have shift : ∀ j, orbit step s (j + 1) = orbit step (step s) j := by
          intro j
          induction j with
          | zero => rfl
          | succ j hj =>
            change step (orbit step s (j + 1)) = step (orbit step (step s) j)
            rw [hj]
        exact ⟨k + 1, by omega, by simpa only [shift] using heq⟩
  obtain ⟨k, hk, heq⟩ := aux (n - card s + 1) s (by have := card_bound s; omega)
  have := card_bound s
  exact ⟨k, by omega, heq⟩

end Facts

def iterate {State : Type} (step : State → State) : Nat → State → State
  | 0, s => s
  | k + 1, s => iterate step k (step s)

theorem iterate_step {State : Type} (step : State → State) (k : Nat) (s : State) :
    iterate step (k + 1) s = step (iterate step k s) := by
  induction k generalizing s with
  | zero => rfl
  | succ k ih => exact ih (step s)

/-- Progress need not count all rows; the driver counts answered identifiers. -/
theorem bounded_progress {State : Type} (step : State → State) (rank : State → Nat)
    (bound : ∀ s, rank s ≤ n) (progress : ∀ s, step s ≠ s → rank s < rank (step s))
    (s : State) :
    ∃ k, k + rank s ≤ n ∧
      iterate step (k + 1) s = iterate step k s := by
  classical
  have aux : ∀ fuel s, n < rank s + fuel →
      ∃ k, k < fuel ∧ iterate step (k + 1) s = iterate step k s := by
    intro fuel
    induction fuel with
    | zero => intro s h; have := bound s; omega
    | succ fuel ih =>
      intro s h
      by_cases eq : step s = s
      · exact ⟨0, by omega, eq⟩
      · have strict := progress s eq
        obtain ⟨k, hk, heq⟩ := ih (step s) (by omega)
        exact ⟨k + 1, by omega, heq⟩
  obtain ⟨k, hk, heq⟩ := aux (n - rank s + 1) s (by have := bound s; omega)
  have := bound s
  exact ⟨k, by omega, heq⟩
end Buildlog
