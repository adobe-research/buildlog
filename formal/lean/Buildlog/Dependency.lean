-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Rules
import Buildlog.Consequences

namespace Buildlog
open Facts

def restrict (region : Facts m) (j : Facts m) : Facts m :=
  fun i => region i ∧ j i

theorem restricted_fact {region s t : Facts m}
    (eq : restrict region s = restrict region t) {i : Fin m} (hi : region i) :
    s i ↔ t i := by
  constructor
  · intro hs; exact ((congrFun eq i).mp ⟨hi, hs⟩).2
  · intro ht; exact ((congrFun eq i).mpr ⟨hi, ht⟩).2

/-- A finite dependency-closure certificate: every positive and lower-stratum
dependency of a protected head is protected; no protected rule reads answers.
It includes negated and aggregate dependencies through `lowerReads` and `reads`.
The loader's graph extraction is an external correctness obligation. -/
structure Protected (p : GroundProgram n m) where
  facts : Facts m
  positive : ∀ s r, facts ((p.rules s).head r) → Sub ((p.rules s).positive r) facts
  lower : ∀ s r, facts ((p.rules s).head r) → Sub ((p.rules s).lowerReads r) facts
  answer_free : ∀ s r, facts ((p.rules s).head r) → ∀ i, ¬ (p.rules s).reads r i

namespace Protected

theorem local_eq (p : GroundProgram n m) (cert : Protected p)
    (s : Fin p.height) (e e' : Facts n) (lower lower' j j' : Facts m)
    (hl : restrict cert.facts lower = restrict cert.facts lower')
    (hj : restrict cert.facts j = restrict cert.facts j') :
    restrict cert.facts ((p.rules s).operator e lower j) =
      restrict cert.facts ((p.rules s).operator e' lower' j') := by
  have rule : ∀ r, cert.facts ((p.rules s).head r) →
      ((p.rules s).fire r e lower j ↔ (p.rules s).fire r e' lower' j') := by
    intro r hp
    have test := (p.rules s).test_ext r e e' lower lower'
      (fun i hi => False.elim (cert.answer_free s r hp i hi))
      (fun i hi => restricted_fact hl (cert.lower s r hp i hi))
    have premises : Sub ((p.rules s).positive r) j ↔ Sub ((p.rules s).positive r) j' := by
      constructor
      · intro h i hi
        exact (restricted_fact hj (cert.positive s r hp i hi)).mp (h i hi)
      · intro h i hi
        exact (restricted_fact hj (cert.positive s r hp i hi)).mpr (h i hi)
    exact and_congr test premises
  apply Facts.ext
  intro i
  constructor
  · rintro ⟨hp, old | ⟨r, head, fire⟩⟩
    · exact ⟨hp, Or.inl ((restricted_fact hj hp).mp old)⟩
    · exact ⟨hp, Or.inr ⟨r, head, (rule r (by simpa only [head] using hp)).mp fire⟩⟩
  · rintro ⟨hp, old | ⟨r, head, fire⟩⟩
    · exact ⟨hp, Or.inl ((restricted_fact hj hp).mpr old)⟩
    · exact ⟨hp, Or.inr ⟨r, head, (rule r (by simpa only [head] using hp)).mpr fire⟩⟩

def independentSlice (p : GroundProgram n m) (cert : Protected p) :
    IndependentSlice p.semantics p.semantics m :=
  { height_eq := rfl
    view := restrict cert.facts
    local_eq := cert.local_eq p }

/-- W5 proved for dependency-certified finite ground rules, not merely
assumed as equality of model projections. -/
theorem explicit_target_requests_answer_free (p : GroundProgram n m) (cert : Protected p)
    (e e' : Facts n) :
    restrict cert.facts (p.semantics.model e) =
      restrict cert.facts (p.semantics.model e') :=
  (cert.independentSlice p).explicit_target_requests_answer_free e e'

end Protected
end Buildlog
