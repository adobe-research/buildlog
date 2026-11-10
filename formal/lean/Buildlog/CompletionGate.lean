-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Dependency
import Buildlog.Provenance

namespace Buildlog
open Facts

/-- Ungated ground rules, the selected propagation clauses, and their
strictly lower completion fact. Both programs share the same finite ranks. -/
structure CompletionProgram (n m : Nat) where
  body : GroundProgram n m
  complete : Fin m
  changed : (s : Fin body.height) → Facts (body.rules s).count
  gate_below : ∀ s r, changed s r → body.rank complete < s.val

namespace CompletionProgram

def gatedRules (p : CompletionProgram n m) (s : Fin p.body.height) : GroundStratum n m :=
  { p.body.rules s with
    test := fun r e lower => (p.body.rules s).test r e lower ∧
      (p.changed s r → lower p.complete)
    lowerReads := fun r i => (p.body.rules s).lowerReads r i ∨
      (p.changed s r ∧ i = p.complete)
    test_ext := by
      intro r e e' lower lower' answers lowerEq
      apply and_congr
      · exact (p.body.rules s).test_ext r e e' lower lower' answers
          (fun i hi => lowerEq i (Or.inl hi))
      · constructor
        · intro gate changed
          exact (lowerEq p.complete (Or.inr ⟨changed, rfl⟩)).mp (gate changed)
        · intro gate changed
          exact (lowerEq p.complete (Or.inr ⟨changed, rfl⟩)).mpr (gate changed) }

def gated (p : CompletionProgram n m) : GroundProgram n m :=
  { p.body with
    rules := p.gatedRules
    head_rank := p.body.head_rank
    positive_rank := p.body.positive_rank
    lower_rank := by
      intro s r i hi
      rcases hi with old | ⟨changed, rfl⟩
      · exact p.body.lower_rank s r i old
      · exact p.gate_below s r changed
    guards := p.body.guards }

/-- W4's reachable slice. It may read answers: unlike W5, W4 is not an
answer-free condition. Positive, negative and aggregate dependencies are
included, and changed heads and completion are excluded. -/
structure Isolation (p : CompletionProgram n m) where
  facts : Facts m
  positive : ∀ s r, facts ((p.body.rules s).head r) →
    Sub ((p.body.rules s).positive r) facts
  lower : ∀ s r, facts ((p.body.rules s).head r) →
    Sub ((p.body.rules s).lowerReads r) facts
  untouched : ∀ s r, facts ((p.body.rules s).head r) → ¬ p.changed s r
  excludes_gate : ¬ facts p.complete
  requests : ∀ i, facts (p.body.requestFact i)

namespace Isolation
variable {n m : Nat} {p : CompletionProgram n m}

theorem local_eq (cert : p.Isolation) (s : Fin p.body.height) (e : Facts n)
    (lower lower' j j' : Facts m)
    (hl : restrict cert.facts lower = restrict cert.facts lower')
    (hj : restrict cert.facts j = restrict cert.facts j') :
    restrict cert.facts ((p.gatedRules s).operator e lower j) =
      restrict cert.facts ((p.body.rules s).operator e lower' j') := by
  have fire : ∀ r, cert.facts ((p.body.rules s).head r) →
      ((p.gatedRules s).fire r e lower j ↔ (p.body.rules s).fire r e lower' j') := by
    intro r hp
    have test := (p.body.rules s).test_ext r e e lower lower'
      (fun _ _ => Iff.rfl) (fun i hi => restricted_fact hl (cert.lower s r hp i hi))
    have premises : Sub ((p.body.rules s).positive r) j ↔
        Sub ((p.body.rules s).positive r) j' := by
      constructor
      · intro h i hi
        exact (restricted_fact hj (cert.positive s r hp i hi)).mp (h i hi)
      · intro h i hi
        exact (restricted_fact hj (cert.positive s r hp i hi)).mpr (h i hi)
    change (((p.body.rules s).test r e lower ∧
      (p.changed s r → lower p.complete)) ∧ Sub ((p.body.rules s).positive r) j) ↔ _
    simp only [cert.untouched s r hp, false_implies, and_true]
    exact and_congr test premises
  apply Facts.ext
  intro i
  constructor
  · rintro ⟨hp, old | ⟨r, head, fired⟩⟩
    · exact ⟨hp, Or.inl ((restricted_fact hj hp).mp old)⟩
    · have head' : (p.body.rules s).head r = i := head
      exact ⟨hp, Or.inr ⟨r, head',
        (fire r (by rw [head']; exact hp)).mp fired⟩⟩
  · rintro ⟨hp, old | ⟨r, head, fired⟩⟩
    · exact ⟨hp, Or.inl ((restricted_fact hj hp).mpr old)⟩
    · exact ⟨hp, Or.inr ⟨r, head,
        (fire r (by simpa only [head] using hp)).mpr fired⟩⟩

theorem models_agree (cert : p.Isolation) (e : Facts n) :
    restrict cert.facts (p.gated.semantics.model e) =
      restrict cert.facts (p.body.semantics.model e) := by
  have stage : ∀ k (h : k ≤ p.body.height),
      restrict cert.facts (p.gated.semantics.stages e k h) =
        restrict cert.facts (p.body.semantics.stages e k h) := by
    intro k
    induction k with
    | zero => intro _; rfl
    | succ k ih =>
      intro h
      let s : Fin p.body.height := ⟨k, by omega⟩
      change restrict cert.facts
        (((p.gatedRules s).positiveSystem e
          (p.gated.semantics.stages e k (by change k ≤ p.body.height; omega))).model
            (p.gated.semantics.stages e k (by change k ≤ p.body.height; omega))) =
        restrict cert.facts
          (((p.body.rules s).positiveSystem e
            (p.body.semantics.stages e k (by change k ≤ p.body.height; omega))).model
              (p.body.semantics.stages e k (by change k ≤ p.body.height; omega)))
      apply Positive.slice_model_eq _ _ _ _ (restrict cert.facts) (ih (by omega))
      intro j j' current
      exact cert.local_eq s e _ _ j j' (ih (by omega)) current
  exact stage p.body.height (Nat.le_refl _)

theorem requests_agree (cert : p.Isolation) (e : Facts n) :
    p.gated.semantics.requests (p.gated.semantics.model e) =
      p.body.semantics.requests (p.body.semantics.model e) :=
  Facts.ext fun i => restricted_fact (cert.models_agree e) (cert.requests i)

def gatedDriver (cert : p.Isolation) (oracle : Driver n n m)
    (check : Facts n → Facts m → Prop) : Driver n n m :=
  { oracle with
    model := p.gated.semantics.model
    requests := p.body.semantics.requests
    badBefore := fun e => check e (restrict cert.facts (p.gated.semantics.model e)) }

def ungatedDriver (cert : p.Isolation) (oracle : Driver n n m)
    (check : Facts n → Facts m → Prop) : Driver n n m :=
  { oracle with
    model := p.body.semantics.model
    requests := p.body.semantics.requests
    badBefore := fun e => check e (restrict cert.facts (p.body.semantics.model e)) }

/-- Domain/tuple/model checks consume the W4 slice; Python row-shape and
primary checks, guarded-first scheduling and β are shared. -/
theorem discovery_agrees (cert : p.Isolation) (oracle : Driver n n m)
    (check : Facts n → Facts m → Prop) :
    ∀ k, (cert.gatedDriver oracle check).rounds k =
      (cert.ungatedDriver oracle check).rounds k := by
  apply Driver.errors_do_not_steer_discovery
  · exact cert.requests_agree
  · rfl
  · rfl
  · rfl
  · funext e
    exact congrArg (check e) (cert.models_agree e)
  · rfl

theorem halt_agrees (cert : p.Isolation) (oracle : Driver n n m)
    (check : Facts n → Facts m → Prop) (e : Facts n) :
    (cert.gatedDriver oracle check).Halt e ↔
      (cert.ungatedDriver oracle check).Halt e := by
  have pre : (cert.gatedDriver oracle check).badBefore e =
      (cert.ungatedDriver oracle check).badBefore e :=
    congrArg (check e) (cert.models_agree e)
  have pending : (cert.gatedDriver oracle check).pending e =
      (cert.ungatedDriver oracle check).pending e := by
    change diff (p.gated.semantics.requests (p.gated.semantics.model e)) (oracle.answered e) =
      diff (p.body.semantics.requests (p.body.semantics.model e)) (oracle.answered e)
    rw [cert.requests_agree e]
  have batch : (cert.gatedDriver oracle check).batch e =
      (cert.ungatedDriver oracle check).batch e := by
    unfold Driver.batch
    rw [pending]
    rfl
  have rows : (cert.gatedDriver oracle check).produced e =
      (cert.ungatedDriver oracle check).produced e := by
    unfold Driver.produced
    rw [batch]
    rfl
  unfold Driver.Halt
  rw [pre, pending, batch, rows]
  rfl

end Isolation

/-- The generated pending and complete clauses at the ground boundary.
Every pending source is a requested, unanswered key. The complete clause
has no positive recursive premise and tests absence of all pending facts.
These are local rule conditions, not assumed final-model equivalence. -/
structure Definition (p : CompletionProgram n m) (answered : Facts n → Facts n) where
  pending : Fin n → Fin m
  complete_stratum : Fin p.body.height
  complete_rule : Fin (p.body.rules complete_stratum).count
  complete_head : (p.body.rules complete_stratum).head complete_rule = p.complete
  complete_positive : (p.body.rules complete_stratum).positive complete_rule = empty
  complete_test : ∀ e lower,
    (p.body.rules complete_stratum).test complete_rule e lower ↔
      ∀ i, ¬ lower (pending i)
  pending_request : ∀ s r i, (p.body.rules s).head r = pending i →
    (p.body.rules s).positive r (p.body.requestFact i)
  pending_unanswered : ∀ s r i e lower, (p.body.rules s).head r = pending i →
    (p.body.rules s).test r e lower → ¬ answered e i

namespace Definition
variable {n m : Nat} {p : CompletionProgram n m}
  {answered : Facts n → Facts n}

theorem no_pending (decl : p.Definition answered) (e : Facts n)
    (done : ∀ i, p.gated.semantics.requests (p.gated.semantics.model e) i → answered e i) :
    ∀ i, ¬ p.gated.semantics.model e (decl.pending i) := by
  intro i present
  obtain ⟨s, r, head, test, premises⟩ := p.gated.model_source e present
  exact decl.pending_unanswered s r i e _ head test.1
    (done i (premises _ (decl.pending_request s r i head)))

theorem complete_holds (decl : p.Definition answered) (e : Facts n)
    (done : ∀ i, p.gated.semantics.requests (p.gated.semantics.model e) i → answered e i) :
    p.gated.semantics.model e p.complete := by
  let s := decl.complete_stratum
  let r := decl.complete_rule
  have notChanged : ¬ p.changed s r := by
    intro changed
    have below := p.gate_below s r changed
    have rank := p.body.head_rank s r
    rw [decl.complete_head] at rank
    omega
  let lower := p.gated.semantics.stages e s.val (Nat.le_of_lt s.isLt)
  have test : (p.body.rules s).test r e lower := by
    apply (decl.complete_test e lower).mpr
    intro i pending
    exact decl.no_pending e done i
      (p.gated.semantics.stages_sub e (Nat.le_of_lt s.isLt)
        (Nat.le_refl _) (Nat.le_of_lt s.isLt) _ pending)
  let sys := (p.gatedRules s).positiveSystem e lower
  have fire : (p.gatedRules s).fire r e lower (sys.model lower) := by
    refine ⟨⟨test, fun changed => False.elim (notChanged changed)⟩, ?_⟩
    change Sub ((p.body.rules s).positive r) (sys.model lower)
    rw [decl.complete_positive]
    exact fun _ h => False.elim h
  have present : sys.model lower p.complete := by
    have step : sys.step (sys.model lower) p.complete :=
      Or.inr ⟨r, decl.complete_head, fire⟩
    rw [sys.fixed lower] at step
    exact step
  exact p.gated.semantics.stages_sub e (k := s.val + 1)
    (l := p.body.height) (by change s.val + 1 ≤ p.body.height; omega)
    (Nat.le_refl _) (by omega) _ present

/-- W1 places the gate strictly below each changed head. Ground rank
stability supplies completion in every evaluated lower input, discharging
the former `final_errors_agree` operator-equality premise. -/
theorem final_models_agree (decl : p.Definition answered) (e : Facts n)
    (done : ∀ i, p.gated.semantics.requests (p.gated.semantics.model e) i → answered e i) :
    p.gated.semantics.model e = p.body.semantics.model e := by
  apply Stratified.final_errors_agree p.gated.semantics p.body.semantics e rfl
  intro s j
  change Fin p.body.height at s
  let lower : Facts m := p.gated.semantics.stages e s.val
    (by change s.val ≤ p.body.height; exact Nat.le_of_lt s.isLt)
  have gate : ∀ r, p.changed s r → lower p.complete := by
    intro r changed
    exact (p.gated.lower_rank_stable e (Nat.le_of_lt s.isLt) (Nat.le_refl _)
      (p.gate_below s r changed) (Nat.le_of_lt s.isLt)).mp (decl.complete_holds e done)
  apply Facts.ext
  intro i
  change (j i ∨ ∃ r, (p.body.rules s).head r = i ∧
    (((p.body.rules s).test r e lower ∧
        (p.changed s r → lower p.complete)) ∧
      Sub ((p.body.rules s).positive r) j)) ↔
    (j i ∨ ∃ r, (p.body.rules s).head r = i ∧
      ((p.body.rules s).test r e lower ∧
          Sub ((p.body.rules s).positive r) j))
  constructor
  · rintro (old | ⟨r, head, ⟨test, _⟩, premises⟩)
    · exact Or.inl old
    · exact Or.inr ⟨r, head, test, premises⟩
  · rintro (old | ⟨r, head, test, premises⟩)
    · exact Or.inl old
    · exact Or.inr ⟨r, head, ⟨test, gate r⟩, premises⟩

end Definition

namespace Isolation
variable {n m : Nat} {p : CompletionProgram n m}

/-- `prop:errors` for certified finite ground rules. Discovery traces and
driver-check stopping agree even on failure. On completion, all non-result
facts agree, including arbitrary error/target/action attribution projections. -/
theorem prop_errors (cert : p.Isolation) (oracle : Driver n n m)
    (check : Facts n → Facts m → Prop) (decl : p.Definition oracle.answered)
    (k : Nat)
    (complete : ∀ i, ¬ (cert.gatedDriver oracle check).pending
      ((cert.gatedDriver oracle check).rounds k) i) :
    (cert.gatedDriver oracle check).rounds k = (cert.ungatedDriver oracle check).rounds k ∧
      ((cert.gatedDriver oracle check).Halt ((cert.gatedDriver oracle check).rounds k) ↔
        (cert.ungatedDriver oracle check).Halt ((cert.ungatedDriver oracle check).rounds k)) ∧
      (cert.gatedDriver oracle check).model ((cert.gatedDriver oracle check).rounds k) =
        (cert.ungatedDriver oracle check).model ((cert.ungatedDriver oracle check).rounds k) := by
  have trace := cert.discovery_agrees oracle check k
  refine ⟨trace, ?_, ?_⟩
  · rw [← trace]
    exact cert.halt_agrees oracle check _
  · rw [← trace]
    apply decl.final_models_agree
    intro i requested
    apply Classical.byContradiction
    intro unanswered
    exact complete i ⟨requested, unanswered⟩

end Isolation
end CompletionProgram
end Buildlog
