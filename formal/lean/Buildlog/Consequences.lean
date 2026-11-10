-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Driver
import Buildlog.Closure

namespace Buildlog
open Facts

/-- Semantic form of dependency-graph nonreachability. The source loader's
syntactic checks are not verified here; this is their local operator obligation.
`view` may include decision relations, or requests/domains/check relations. -/
structure IndependentSlice (p q : Stratified n m) (d : Nat) where
  height_eq : p.height = q.height
  view : Facts m → Facts d
  local_eq : ∀ (s : Fin p.height) (e e' : Facts n) lower lower' j j',
    view lower = view lower' → view j = view j' →
    view ((p.stratum s e lower).step j) =
      view ((q.stratum ⟨s.val, by simpa only [← height_eq] using s.isLt⟩ e' lower').step j')

namespace IndependentSlice

theorem stages_index_eq (p : Stratified n m) (e : Facts n)
    {k l : Nat} (hk : k ≤ p.height) (hl : l ≤ p.height) (h : k = l) :
    p.stages e k hk = p.stages e l hl := by
  cases h
  rfl

theorem models_agree {p q : Stratified n m} (s : IndependentSlice p q d) (e e' : Facts n) :
    s.view (p.model e) = s.view (q.model e') := by
  have stage : ∀ k (h : k ≤ p.height),
      s.view (p.stages e k h) =
        s.view (q.stages e' k (by simpa only [← s.height_eq] using h)) := by
    intro k
    induction k with
    | zero => intro h; rfl
    | succ k ih =>
      intro h
      simp only [Stratified.stages]
      apply Positive.slice_model_eq _ _ _ _ s.view (ih (by omega))
      intro j j' hj
      exact s.local_eq ⟨k, by omega⟩ e e' _ _ j j' (ih (by omega)) hj
  unfold Stratified.model
  have eq := stage p.height (Nat.le_refl _)
  exact eq.trans (congrArg s.view (stages_index_eq q e' _ _ s.height_eq))

/-- W5 at the semantic interface: answers cannot affect the decision slice. -/
theorem explicit_target_requests_answer_free {p : Stratified n m}
    (w5 : IndependentSlice p p d) (e e' : Facts n) :
    w5.view (p.model e) = w5.view (p.model e') :=
  w5.models_agree e e'

end IndependentSlice

namespace Driver

/-- W4's semantic interface includes requests, domains and all model-based
checks. Python answer-shape checks are shared. This proves trace equality,
including the guarded-first schedule and failing checks. -/
theorem errors_do_not_steer_discovery (p q : Driver n a m)
    (req : ∀ e, p.requests (p.model e) = q.requests (q.model e))
    (guard : p.guarded = q.guarded) (primary : p.primary = q.primary)
    (oracle : p.beta = q.beta) (pre : p.badBefore = q.badBefore)
    (post : p.badAfter = q.badAfter) :
    ∀ k, p.rounds k = q.rounds k := by
  have step_eq : p.step = q.step := by
    funext e
    have ans : p.answered e = q.answered e := by unfold answered; rw [primary]
    have pending_eq : p.pending e = q.pending e := by unfold pending; rw [req, ans]
    have batch_eq : p.batch e = q.batch e := by unfold batch; rw [pending_eq, guard]
    have rows : p.produced e = q.produced e := by unfold produced; rw [batch_eq, oracle]
    have halt : p.Halt e ↔ q.Halt e := by
      unfold Halt
      rw [pre, pending_eq, post, batch_eq, rows]
    unfold step
    rw [propext halt, rows]
  intro k
  unfold rounds
  rw [step_eq]

theorem outcome_deterministic (p : Driver n a m) (trace : Nat → Facts a)
    (zero : trace 0 = empty) (next : ∀ k, trace (k + 1) = p.step (trace k))
    {Outcome : Type} (render : Facts a → Facts m → Outcome) (k : Nat) :
    render (trace k) (p.model (trace k)) =
      render (p.rounds k) (p.model (p.rounds k)) := by
  rw [p.fresh_determinism trace zero next k]

end Driver

namespace Stratified

/-- The final completion gate must be true in all changed error strata.
This local equality is the explicit discharge obligation for deleting it;
W4 alone proves discovery independence, not final-error equivalence.
`CompletionProgram.Definition.final_models_agree` discharges this obligation
from ground rules, generated completion and the gate/head rank ordering. -/
theorem final_errors_agree (p q : Stratified n m) (e : Facts n)
    (height : p.height = q.height)
    (complete : ∀ (s : Fin p.height) j,
      (p.stratum s e (p.stages e s.val (Nat.le_of_lt s.isLt))).step j =
        (q.stratum ⟨s.val, by simpa only [← height] using s.isLt⟩ e
          (p.stages e s.val (Nat.le_of_lt s.isLt))).step j) :
    p.model e = q.model e := by
  have stage : ∀ k (h : k ≤ p.height),
      p.stages e k h = q.stages e k (by simpa only [← height] using h) := by
    intro k
    induction k with
    | zero => intro h; rfl
    | succ k ih =>
      intro h
      simp only [stages, ← ih (by omega)]
      apply Positive.model_eq_of_agree
      intro j _
      exact complete _ j
  unfold model
  exact (stage p.height (Nat.le_refl _)).trans
    (IndependentSlice.stages_index_eq q e _ _ height)

end Stratified
end Buildlog
