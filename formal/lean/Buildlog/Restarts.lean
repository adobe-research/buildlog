-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Finite

namespace Buildlog
open Facts

/-- Normalized directory demand D, not the resolver's loaded closure C.
Finite directories and a deterministic pass are assumptions. -/
structure Restarts (n : Nat) where
  initial : Facts n
  demand : Facts n → Facts n

namespace Restarts

def step (p : Restarts n) (s : Facts n) : Facts n := union s (p.demand s)
def roots (p : Restarts n) (k : Nat) : Facts n := orbit p.step p.initial k
def Supported (p : Restarts n) (s : Facts n) : Prop :=
  ∀ i, s i → ¬ p.initial i → p.demand s i

theorem initial_sub (p : Restarts n) (k : Nat) : Sub p.initial (p.roots k) := by
  induction k with
  | zero => exact sub_refl _
  | succ k ih => exact fun i hi => Or.inl (ih i hi)

theorem roots_sub (p : Restarts n) {k l : Nat} (h : k ≤ l) :
    Sub (p.roots k) (p.roots l) := by
  induction l with
  | zero =>
    have : k = 0 := by omega
    subst k; exact sub_refl _
  | succ l ih =>
    by_cases eq : k = l + 1
    · subst k; exact sub_refl _
    · exact sub_trans (ih (by omega)) (fun _ hi => Or.inl hi)

theorem restarts_terminate (p : Restarts n) :
    ∃ k, k + card p.initial ≤ n ∧
      Sub (p.demand (p.roots k)) (p.roots k) ∧
      (p.roots k = union p.initial (p.demand (p.roots k)) ∨
        ¬ p.Supported (p.roots k)) := by
  classical
  obtain ⟨k, hk, eq⟩ := finite_progress p.step (fun _ _ hi => Or.inl hi) p.initial
  have closed : Sub (p.demand (p.roots k)) (p.roots k) := by
    intro i hi
    have hs : p.roots (k + 1) i := Or.inr hi
    exact (congrFun eq i).mp hs
  refine ⟨k, hk, closed, ?_⟩
  by_cases supported : p.Supported (p.roots k)
  · left
    apply sub_antisymm
    · intro i hi
      by_cases init : p.initial i
      · exact Or.inl init
      · exact Or.inr (supported i hi init)
    · intro i hi
      rcases hi with init | dem
      · exact p.initial_sub k i init
      · exact closed i dem
  · exact Or.inr supported

/-- All consistent passes are functions of their root set. -/
theorem restart_determinism (p : Restarts n) (trace : Nat → Facts n)
    (zero : trace 0 = p.initial) (next : ∀ k, trace (k + 1) = p.step (trace k)) :
    ∀ k, trace k = p.roots k := by
  intro k
  induction k with
  | zero => exact zero
  | succ k ih => rw [next, ih]; rfl

theorem monotone_support (p : Restarts n)
    (mono : ∀ {s t}, Sub s t → Sub (p.demand s) (p.demand t)) :
    ∀ k, p.Supported (p.roots k) := by
  intro k
  induction k with
  | zero => intro i hi hn; exact False.elim (hn hi)
  | succ k ih =>
    intro i hi hn
    rcases hi with prev | dem
    · exact mono (fun _ hj => Or.inl hj) i (ih i prev hn)
    · exact mono (fun _ hj => Or.inl hj) i dem

theorem roots_sub_closed (p : Restarts n)
    (mono : ∀ {s t}, Sub s t → Sub (p.demand s) (p.demand t))
    {t : Facts n} (init : Sub p.initial t) (closed : Sub (p.demand t) t) :
    ∀ k, Sub (p.roots k) t := by
  intro k
  induction k with
  | zero => exact init
  | succ k ih =>
    intro i hi
    rcases hi with prev | dem
    · exact ih i prev
    · exact closed i (mono ih i dem)

theorem least_restart_roots (p : Restarts n)
    (mono : ∀ {s t}, Sub s t → Sub (p.demand s) (p.demand t)) :
    ∃ k, k + card p.initial ≤ n ∧
      p.roots k = union p.initial (p.demand (p.roots k)) ∧
      (∀ t, t = union p.initial (p.demand t) → Sub (p.roots k) t) := by
  obtain ⟨k, hk, closed, outcome⟩ := p.restarts_terminate
  have eq := outcome.resolve_right (fun h => h (p.monotone_support mono k))
  refine ⟨k, hk, eq, ?_⟩
  intro t ht
  refine p.roots_sub_closed mono ?_ ?_ k
  · intro i hi; rw [ht]; exact Or.inl hi
  · intro i hi; rw [ht]; exact Or.inr hi

/-- An arbitrary order/grouping is represented by a trace which only adds
pending packages, never deletes roots, and eventually stops. -/
theorem grouping_independent (p : Restarts n)
    (mono : ∀ {s t}, Sub s t → Sub (p.demand s) (p.demand t))
    (trace : Nat → Facts n) (zero : trace 0 = p.initial)
    (grows : ∀ k, Sub (trace k) (trace (k + 1)))
    (adds : ∀ k i, trace (k + 1) i → trace k i ∨ p.demand (trace k) i)
    (k : Nat) (done : Sub (p.demand (trace k)) (trace k)) :
    ∀ j, Sub (p.demand (p.roots j)) (p.roots j) → trace k = p.roots j := by
  have below : ∀ t, Sub p.initial t → Sub (p.demand t) t →
      ∀ l, Sub (trace l) t := by
    intro t hi hc l
    induction l with
    | zero => simpa only [zero] using hi
    | succ l ih =>
      intro i h
      rcases adds l i h with old | new
      · exact ih i old
      · exact hc i (mono ih i new)
  have initial : ∀ l, Sub p.initial (trace l) := by
    intro l
    induction l with
    | zero => simpa only [zero] using sub_refl p.initial
    | succ l ih => exact sub_trans ih (grows l)
  intro j hj
  exact sub_antisymm (below _ (p.initial_sub j) hj k)
    (p.roots_sub_closed mono (initial k) done j)

/-- Finite progress for arbitrary grouping, provided every nonterminal step
adds at least one package. -/
theorem grouping_terminates (p : Restarts n) (choose : Facts n → Facts n)
    (grows : ∀ s, Sub s (choose s))
    (stop : ∀ s, choose s = s → Sub (p.demand s) s) :
    ∃ k, k + card p.initial ≤ n ∧
      Sub (p.demand (orbit choose p.initial k)) (orbit choose p.initial k) := by
  obtain ⟨k, hk, heq⟩ := finite_progress choose grows p.initial
  exact ⟨k, hk, stop _ heq⟩

end Restarts
end Buildlog
