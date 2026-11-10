-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Semantics

namespace Buildlog
open Facts

namespace Stratified

/-- Fixed correct unguarded answers. The type indexes fixed oracle bundles:
membership of an identifier means inclusion of its entire α answer. -/
def guardedRequests (p : Stratified n m) (f e : Facts n) : Facts n :=
  fun i => p.guarded i ∧ p.requests (p.model (union e f)) i

def Closed (p : Stratified n m) (f e : Facts n) : Prop :=
  Sub e p.guarded ∧ Sub (p.guardedRequests f e) e

theorem closed_exists (p : Stratified n m) (f : Facts n) :
    ∃ e, p.Closed f e :=
  ⟨p.guarded, sub_refl _, fun _ hi => hi.1⟩

theorem closed_models (p : Stratified n m) (f e₁ e₂ : Facts n)
    (h₁ : p.Closed f e₁) (h₂ : p.Closed f e₂) :
    p.model (union e₁ f) = p.model (union e₂ f) := by
  have one : ∀ a b, p.Closed f a → p.Closed f b →
      p.model (union a f) = p.model (union (union a b) f) := by
    intro a b ha hb
    have inert := p.inert_before (union a f) (diff b a)
      (fun i hi => hb.1 i hi.1)
      (fun i hi hr => hi.2 (ha.2 i ⟨hb.1 i hi.1, hr⟩))
    have rearrange : union (union a f) (diff b a) = union (union a b) f := by
      apply Facts.ext
      intro i
      simp only [union, diff]
      grind
    simpa only [rearrange] using inert
  have comm : union (union e₂ e₁) f = union (union e₁ e₂) f := by
    apply Facts.ext
    intro i
    simp only [union]
    grind
  exact (one e₁ e₂ h₁ h₂).trans (by simpa only [comm] using (one e₂ e₁ h₂ h₁).symm)

noncomputable def leastAnswers (p : Stratified n m) (f : Facts n) : Facts n :=
  p.guardedRequests f p.guarded

theorem least_sub_closed (p : Stratified n m) (f e : Facts n)
    (he : p.Closed f e) : Sub (p.leastAnswers f) e := by
  intro i hi
  apply he.2 i
  change p.guarded i ∧ p.requests (p.model (union e f)) i
  have eq := p.closed_models f p.guarded e ⟨sub_refl _, fun _ hi => hi.1⟩ he
  exact ⟨hi.1, by simpa only [← eq] using hi.2⟩

theorem least_model (p : Stratified n m) (f : Facts n) :
    p.model (union (p.leastAnswers f) f) = p.model (union p.guarded f) := by
  have rearrange :
      union (union (p.leastAnswers f) f) (diff p.guarded (p.leastAnswers f)) =
        union p.guarded f := by
    apply Facts.ext
    intro i
    simp only [union, diff, leastAnswers, guardedRequests]
    grind
  have inert := p.inert_answers (union (p.leastAnswers f) f)
    (diff p.guarded (p.leastAnswers f)) (fun _ hi => hi.1)
    (Or.inr (by
      intro i hi hr
      apply hi.2
      exact ⟨hi.1, by simpa only [rearrange] using hr⟩))
  simpa only [rearrange] using inert

/-- Leastness is not obtained from monotonicity in answers. -/
theorem least_closed_guarded_answers (p : Stratified n m) (f : Facts n) :
    p.Closed f (p.leastAnswers f) ∧
    (∀ e, p.Closed f e → Sub (p.leastAnswers f) e) ∧
    p.guardedRequests f (p.leastAnswers f) = p.leastAnswers f ∧
    (∀ e, p.Closed f e →
      p.model (union e f) = p.model (union (p.leastAnswers f) f)) := by
  have req_eq : p.guardedRequests f (p.leastAnswers f) = p.leastAnswers f := by
    change (fun i => p.guarded i ∧
      p.requests (p.model (union (p.leastAnswers f) f)) i) = p.leastAnswers f
    rw [p.least_model f]
    rfl
  have closed : p.Closed f (p.leastAnswers f) :=
    ⟨fun _ hi => hi.1, by rw [req_eq]; exact sub_refl _⟩
  exact ⟨closed, fun e he => p.least_sub_closed f e he, req_eq,
    fun e he => p.closed_models f e _ he closed⟩

theorem least_unique (p : Stratified n m) (f e : Facts n)
    (he : p.Closed f e) (hleast : ∀ other, p.Closed f other → Sub e other) :
    e = p.leastAnswers f :=
  sub_antisymm (hleast _ (p.least_closed_guarded_answers f).1) (p.least_sub_closed f e he)

/-- Semantic phase lemma once confirmation has established correct fixed
bundles and absence of all unconfirmed requests. Fingerprints, watches and
the FL confirmation induction are not verified by this theorem. -/
theorem round_model (p : Stratified n m) (f c x : Facts n)
    (gc : Sub c p.guarded) (gx : Sub x p.guarded)
    (unused : ∀ i, x i → ¬ p.requests (p.model (union (union c f) x)) i)
    (answered : ∀ i, p.guarded i →
      p.requests (p.model (union (union c f) x)) i → c i) :
    p.Closed f c ∧
      p.model (union (union c f) x) = p.model (union (p.leastAnswers f) f) := by
  have inert := p.inert_answers (union c f) x gx (Or.inr unused)
  have closed : p.Closed f c := ⟨gc, by
    intro i hi
    apply answered i hi.1
    simpa only [← inert] using hi.2⟩
  exact ⟨closed, inert.symm.trans ((p.least_closed_guarded_answers f).2.2.2 c closed)⟩

end Stratified
end Buildlog
