-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.CompletionGate

namespace Buildlog.Examples
open Facts

/-- A derived completion fact in stratum 0 and a gated error in stratum 1.
There are no requests; the changed propagation rule is not empty. -/
def gateExample : CompletionProgram 0 2 :=
  { body :=
      { height := 2
        rank := Fin.val
        rules := fun s =>
          { count := 1
            head := fun _ => s
            positive := fun _ => empty
            test := fun _ _ _ => True
            reads := fun _ => empty
            lowerReads := fun _ => empty
            test_ext := fun _ _ _ _ _ _ _ => Iff.rfl }
        head_rank := fun _ _ => rfl
        positive_rank := fun _ _ _ h => False.elim h
        lower_rank := fun _ _ _ h => False.elim h
        requestFact := Fin.elim0
        guarded := empty
        guards := fun _ _ _ h _ => False.elim h }
    complete := 0
    changed := fun s _ => s = 1
    gate_below := by intro s _ hs; subst s; decide }

def gateDefinition : gateExample.Definition (fun _ => empty) :=
  { pending := Fin.elim0
    complete_stratum := ⟨0, by decide⟩
    complete_rule := ⟨0, by decide⟩
    complete_head := rfl
    complete_positive := rfl
    complete_test := fun _ _ =>
      ⟨fun _ i => Fin.elim0 i, fun _ => True.intro⟩
    pending_request := fun _ _ i => Fin.elim0 i
    pending_unanswered := fun _ _ i => Fin.elim0 i }

def gateIsolation : gateExample.Isolation :=
  { facts := empty
    positive := fun _ _ h => False.elim h
    lower := fun _ _ h => False.elim h
    untouched := fun _ _ h => False.elim h
    excludes_gate := fun h => h
    requests := Fin.elim0 }

def gateOracle : Driver 0 0 2 :=
  { model := fun _ => empty
    requests := fun _ => empty
    guarded := empty
    primary := fun _ _ => False
    beta := Fin.elim0
    badBefore := fun _ => False
    badAfter := fun _ _ _ => False
    total_if_checked := fun _ _ _ _ _ i => Fin.elim0 i }

theorem changed_gate_example :
    gateExample.changed ⟨1, by decide⟩ ⟨0, by decide⟩ := rfl

theorem gate_completion_derived : gateExample.gated.semantics.model empty 0 :=
  gateDefinition.complete_holds empty (fun i => Fin.elim0 i)

theorem gate_final_errors_agree :
    gateExample.gated.semantics.model empty = gateExample.body.semantics.model empty :=
  gateDefinition.final_models_agree empty (fun i => Fin.elim0 i)

theorem gate_discovery_agrees :
    ∀ k, (gateIsolation.gatedDriver gateOracle (fun _ _ => False)).rounds k =
      (gateIsolation.ungatedDriver gateOracle (fun _ _ => False)).rounds k :=
  gateIsolation.discovery_agrees gateOracle (fun _ _ => False)

end Buildlog.Examples
