-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Rules

namespace Buildlog
open Facts

namespace GroundStratum

theorem model_source (p : GroundStratum n m) (e : Facts n) (lower : Facts m)
    {i : Fin m} (present : (p.positiveSystem e lower).model lower i) :
    lower i ∨ ∃ r, p.head r = i ∧
      p.fire r e lower ((p.positiveSystem e lower).model lower) := by
  have sources : ∀ k i, orbit (p.operator e lower) lower k i →
      lower i ∨ ∃ r, p.head r = i ∧
        p.fire r e lower ((p.positiveSystem e lower).model lower) := by
    intro k
    induction k with
    | zero => intro i hi; exact Or.inl hi
    | succ k ih =>
      intro i hi
      rcases hi with old | ⟨r, head, test, premises⟩
      · exact ih i old
      · exact Or.inr ⟨r, head, test,
          sub_trans premises ((p.positiveSystem e lower).iterate_sub_model lower k)⟩
  exact sources _ i present

end GroundStratum

namespace GroundProgram

/-- Every derived fact has a finite rule source; its positive premises hold
in the final model. No supported-model or arbitrary-set assumption is used. -/
theorem model_source (p : GroundProgram n m) (e : Facts n)
    {i : Fin m} (present : p.semantics.model e i) :
    ∃ s r, (p.rules s).head r = i ∧
      (p.rules s).test r e (p.semantics.stages e s.val (Nat.le_of_lt s.isLt)) ∧
      Sub ((p.rules s).positive r) (p.semantics.model e) := by
  have source : ∀ k (h : k ≤ p.height) i, p.semantics.stages e k h i →
      ∃ s r, (p.rules s).head r = i ∧
        (p.rules s).test r e (p.semantics.stages e s.val (Nat.le_of_lt s.isLt)) ∧
        Sub ((p.rules s).positive r) (p.semantics.model e) := by
    intro k
    induction k with
    | zero => intro _ _ hi; exact False.elim hi
    | succ k ih =>
      intro h i hi
      let s : Fin p.height := ⟨k, by omega⟩
      obtain old | ⟨r, head, test, premises⟩ :=
        (p.rules s).model_source e (p.semantics.stages e k (by
          change k ≤ p.height; omega)) hi
      · exact ih (by omega) i old
      · refine ⟨s, r, head, test, sub_trans premises ?_⟩
        exact p.semantics.stages_sub (e := e) (k := k + 1)
          (l := p.height) (by change k + 1 ≤ p.height; exact h)
          (Nat.le_refl _) (by omega)
  exact source p.height (Nat.le_refl _) i present

/-- Higher strata cannot create a fact whose defining head rank is below
the cutoff. Thus final completion transfers to every changed rule's lower
input, rather than being substituted into an arbitrary interpretation. -/
theorem lower_rank_stable (p : GroundProgram n m) (e : Facts n)
    {i : Fin m} {k l : Nat} (hk : k ≤ p.height) (hl : l ≤ p.height)
    (earlier : p.rank i < k) (later : k ≤ l) :
    p.semantics.stages e l hl i ↔ p.semantics.stages e k hk i := by
  constructor
  · intro present
    induction l with
    | zero => omega
    | succ l ih =>
      by_cases same : k = l + 1
      · subst k; exact present
      · have cutoff : k ≤ l := by omega
        let s : Fin p.height := ⟨l, by omega⟩
        obtain old | ⟨r, head, _, _⟩ :=
          (p.rules s).model_source e
            (p.semantics.stages e l (by change l ≤ p.height; omega)) present
        · exact ih (by omega) cutoff old
        · have rank := p.head_rank s r
          rw [head] at rank
          change p.rank i = l at rank
          omega
  · exact p.semantics.stages_sub e hk hl later i

end GroundProgram
end Buildlog
