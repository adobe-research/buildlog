-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Driver

namespace Buildlog.Examples
open Facts

def withdrawingRequests (e : Facts 2) : Facts 2 :=
  fun i => i = 0 ∨ (i = 1 ∧ ¬ e 0)

/-- Paper's a/b example: the b request is withdrawn by a's answer. -/
theorem request_operator_not_monotone :
    ¬ (∀ {s t : Facts 2}, Sub s t →
      Sub (withdrawingRequests s) (withdrawingRequests t)) := by
  intro monotone
  have bad := monotone (s := empty) (t := full) (fun _ h => False.elim h) 1
    (Or.inr ⟨rfl, fun h => h⟩)
  simp [withdrawingRequests, full] at bad

def withdrawalDriver : Driver 2 2 2 :=
  { model := withdrawingRequests
    requests := id
    guarded := full
    primary := fun i r => i = r
    beta := fun i _ r => i = r
    badBefore := fun _ => False
    badAfter := fun _ _ _ => False
    total_if_checked := by
      intro _ batch rows eq _ i hi
      refine ⟨i, rfl, ?_⟩
      rw [eq]
      exact ⟨i, hi, rfl⟩ }

theorem example_fresh_termination :
    ∃ k, k ≤ 2 ∧ withdrawalDriver.Halt (withdrawalDriver.rounds k) :=
  withdrawalDriver.fresh_termination

/-- Leastness of closed guarded answers must not be transferred to unguarded
answers. Either singleton closes these mutually-negated requests. -/
def unguardedRequests (e : Facts 2) : Facts 2 :=
  fun i => (i = 0 ∧ ¬ e 1) ∨ (i = 1 ∧ ¬ e 0)

def onlyA : Facts 2 := fun i => i = 0
def onlyB : Facts 2 := fun i => i = 1

theorem unguarded_has_no_least_closed :
    ¬ ∃ least : Facts 2,
      Sub (unguardedRequests least) least ∧
      (∀ e, Sub (unguardedRequests e) e → Sub least e) := by
  have ca : Sub (unguardedRequests onlyA) onlyA := by
    intro i hi
    rcases hi with ⟨ha, _⟩ | ⟨_, hn⟩
    · exact ha
    · exact False.elim (hn rfl)
  have cb : Sub (unguardedRequests onlyB) onlyB := by
    intro i hi
    rcases hi with ⟨_, hn⟩ | ⟨hb, _⟩
    · exact False.elim (hn rfl)
    · exact hb
  rintro ⟨least, closed, minimal⟩
  have noA : ¬ least 0 := by
    intro h
    have := minimal onlyB cb 0 h
    simp [onlyB] at this
  have noB : ¬ least 1 := by
    intro h
    have := minimal onlyA ca 1 h
    simp [onlyA] at this
  exact noA (closed 0 (Or.inl ⟨rfl, noB⟩))

end Buildlog.Examples
