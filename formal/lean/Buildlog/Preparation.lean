-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Closure
import Buildlog.Driver
import Buildlog.CacheSeeding

namespace Buildlog
open Facts

/-- A fixed finite graph of declared roots, metadata includes and rule pointers.
The complete graph is determined before inference, without probe answers or
target demand. Enumeration completeness, parsing and certificates are external
obligations; this is not a verified filesystem traversal. -/
structure Preparation (n : Nat) where
  initial : Facts n
  links : Fin n → Facts n

namespace Preparation

def operator (p : Preparation n) : Positive n :=
  { step := fun s => union s (fun j => ∃ i, s i ∧ p.links i j)
    grows := fun _ _ hi => Or.inl hi
    mono := by
      intro s t h j hj
      rcases hj with old | ⟨i, hi, link⟩
      · exact Or.inl (h j old)
      · exact Or.inr ⟨i, h i hi, link⟩ }

noncomputable def closure (p : Preparation n) : Facts n :=
  p.operator.model p.initial

def Closed (p : Preparation n) (s : Facts n) : Prop :=
  ∀ i, s i → Sub (p.links i) s

theorem terminates (p : Preparation n) :
    ∃ k, k + card p.initial ≤ n ∧
      orbit p.operator.step p.initial (k + 1) =
        orbit p.operator.step p.initial k :=
  ⟨p.operator.stop p.initial, p.operator.stop_spec p.initial⟩

theorem initial_sub (p : Preparation n) : Sub p.initial p.closure :=
  p.operator.base_sub_iterate p.initial _

theorem closed (p : Preparation n) : p.Closed p.closure := by
  intro i hi j link
  have step : p.operator.step p.closure j := Or.inr ⟨i, hi, link⟩
  simpa only [closure, p.operator.fixed] using step

theorem least (p : Preparation n) {s : Facts n}
    (initial : Sub p.initial s) (closed : p.Closed s) : Sub p.closure s := by
  apply p.operator.least initial
  apply sub_antisymm
  · intro j hj
    rcases hj with old | ⟨i, hi, link⟩
    · exact old
    · exact closed i hi j link
  · exact p.operator.grows s

end Preparation

namespace Stratified

private theorem union_empty_right (e : Facts n) : union e empty = e := by
  apply Facts.ext
  intro i
  exact ⟨fun h => h.elim id False.elim, Or.inl⟩

theorem all_guarded_closed (p : Stratified n m)
    (all : ∀ i, p.guarded i) (e : Facts n) :
    p.Closed empty e ↔ Sub (p.requests (p.model e)) e := by
  constructor
  · intro h i hi
    apply h.2 i
    exact ⟨all i, by simpa only [union_empty_right] using hi⟩
  · intro h
    refine ⟨fun i _ => all i, ?_⟩
    intro i hi
    apply h i
    simpa only [union_empty_right] using hi.2

/-- With a fixed prepared program and only guarded environmental kinds,
there is no package-answer phase: F is empty. Leastness still follows from
inertness, not monotonicity of requests. -/
theorem fixed_union_closure (p : Stratified n m) (all : ∀ i, p.guarded i) :
    Sub (p.requests (p.model (p.leastAnswers empty))) (p.leastAnswers empty) ∧
      (∀ e, Sub (p.requests (p.model e)) e → Sub (p.leastAnswers empty) e) ∧
      (∀ e, Sub (p.requests (p.model e)) e →
        p.model e = p.model (p.leastAnswers empty)) := by
  have h := p.least_closed_guarded_answers empty
  refine ⟨(p.all_guarded_closed all _).mp h.1, ?_, ?_⟩
  · intro e he
    exact h.2.1 e ((p.all_guarded_closed all e).mpr he)
  · intro e he
    simpa only [union_empty_right] using
      h.2.2.2 e ((p.all_guarded_closed all e).mpr he)

end Stratified

theorem Driver.all_guarded_batch (p : Driver n a m)
    (all : ∀ i, p.guarded i) (e : Facts a) : p.batch e = p.pending e := by
  classical
  unfold Driver.batch
  split
  · exact Facts.ext fun i => ⟨fun h => h.1, fun h => ⟨h, all i⟩⟩
  · rfl

/-- FC/FL confirmation and absent tentative requests remain certificate
premises. No package schedule or composition-restart certificate is needed
for this all-guarded specialization. -/
theorem CachePhase.fixed_union_agreement {p : BundleProgram n a m}
    {Key : Type} (c : CachePhase p Key) (all : ∀ i, p.guarded i) :
    c.f = empty ∧ c.model = p.phaseModel empty := by
  have none : c.f = empty := by
    apply Facts.ext
    intro i
    exact ⟨fun hi => False.elim (c.f_unguarded i hi (all i)), False.elim⟩
  refine ⟨none, ?_⟩
  simpa only [none, BundleProgram.phaseModel] using c.reduced.2

end Buildlog
