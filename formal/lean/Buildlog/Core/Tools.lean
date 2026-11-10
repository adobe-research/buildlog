-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Core.Toolchain

namespace Buildlog.Core

/-- Open action and completed naming/artifact relations. `otherNeeded` is
the completed contribution of non-tool needed rules (requests, dependencies,
tests, linkage instances), not a claim that those rules are transcribed. -/
structure ToolInput where
  tc : ToolchainInput
  actionRun : Symbol → Int → Symbol → Symbol → Symbol → Prop
  actionRunItem : Symbol → Int → Int → Symbol → Symbol → Prop
  actionTcTool : Symbol → Symbol → Symbol → Prop
  targetToolchain : Symbol → Symbol → Prop
  targetId : Symbol → Prop
  idScope : Symbol → Symbol → Symbol → Prop
  alias : Symbol → Symbol → Prop
  qualify : Symbol → Symbol → Symbol
  runArtifact : Symbol → Config → Symbol → Prop
  otherNeeded : Symbol → Config → Prop

namespace ToolInput

-- Frozen id_scope and target_id; the shipped derived naming clauses follow.
inductive Scoped (i : ToolInput) : Symbol → Prop
  | scope : i.idScope t q n → Scoped i t

inductive ScopeName (i : ToolInput) : Symbol → Symbol → Prop
  | scope : i.idScope t q n → ScopeName i q n

inductive ProjectName (i : ToolInput) : Symbol → Prop
  | target : i.targetId r → ¬ Scoped i r → ProjectName i r

inductive NameCandidate (i : ToolInput) : Symbol → Symbol → Prop
  | target : i.targetId t → NameCandidate i t t
  | alias : i.alias n m → NameCandidate i m t → NameCandidate i n t

-- Both typed-placeholder clauses of tool_ref, including list items and use.
inductive Ref (i : ToolInput) : Symbol → Config → Symbol → Symbol → Symbol → Prop
  | scalar : i.actionRun a rank pre r use → i.tc.targetAction t c a → Ref i t c a r use
  | item : i.actionRunItem a rank index r use → i.tc.targetAction t c a → Ref i t c a r use

/-- `name_candidate(r,g)` plus `1 = count : { name_candidate(r,_) }`.
The explicit candidate supplies nonemptiness; equality of all candidates
encodes exactly one distinct tuple, rather than one derivation. -/
def UniqueCandidate (i : ToolInput) (r g : Symbol) : Prop :=
  NameCandidate i r g ∧ ∀ other, NameCandidate i r other → other = g

inductive Resolves (i : ToolInput) : Symbol → Symbol → Symbol → Prop
  | scopedLocal : Ref i t c a r use → i.idScope t q name → ScopeName i q r →
      Resolves i t r (i.qualify q r)
  | projectLocal : Ref i t c a r use → ¬ Scoped i t → ProjectName i r → Resolves i t r r
  | scopedFallback : Ref i t c a r use → i.idScope t q name → ¬ ScopeName i q r →
      i.UniqueCandidate r g → Resolves i t r g
  | projectFallback : Ref i t c a r use → ¬ Scoped i t → ¬ ProjectName i r →
      i.UniqueCandidate r g → Resolves i t r g

inductive Resolved (i : ToolInput) : Symbol → Symbol → Prop
  | target : Resolves i t r g → Resolved i t r

mutual
  inductive Needed (i : ToolInput) : Symbol → Config → Prop
    | other : i.otherNeeded t c → Needed i t c
    | tool : Dependency i owner c g → ConfigInput.Exec i.tc.config c e → Needed i g e

  inductive Dependency (i : ToolInput) : Symbol → Config → Symbol → Prop
    | reference : Ref i t c a r use → Resolves i t r g → Dependency i t c g
    | builtToolchain {tc : Symbol} : Needed i t c → i.targetToolchain t tc →
        i.tc.toolTarget tc role g → Dependency i t c g
end

theorem direct_dependencies_exact (i : ToolInput) {t g : Symbol} {c : Config} :
    Dependency i t c g ↔
      (∃ a r use, Ref i t c a r use ∧ Resolves i t r g) ∨
      (Needed i t c ∧ ∃ tc role, i.targetToolchain t tc ∧ i.tc.toolTarget tc role g) := by
  constructor
  · intro h
    cases h with
    | reference ref resolves => exact Or.inl ⟨_, _, _, ref, resolves⟩
    | builtToolchain need chosen role => exact Or.inr ⟨need, _, _, chosen, role⟩
  · rintro (⟨a, r, use, ref, resolves⟩ | ⟨need, tc, role, chosen, program⟩)
    · exact Dependency.reference ref resolves
    · exact Dependency.builtToolchain need chosen program

def DirectToolNeed (i : ToolInput) (g : Symbol) (e : Config) : Prop :=
  ∃ owner c, Dependency i owner c g ∧ ConfigInput.Exec i.tc.config c e

theorem direct_tool_need_exact (i : ToolInput) {g : Symbol} {e : Config} :
    i.DirectToolNeed g e ↔
      ∃ owner c, ConfigInput.Exec i.tc.config c e ∧
        ((∃ a r use, Ref i owner c a r use ∧ Resolves i owner r g) ∨
          (Needed i owner c ∧
            ∃ tc role, i.targetToolchain owner tc ∧ i.tc.toolTarget tc role g)) := by
  constructor
  · rintro ⟨owner, c, dep, exec⟩
    exact ⟨owner, c, exec, i.direct_dependencies_exact.mp dep⟩
  · rintro ⟨owner, c, exec, source⟩
    exact ⟨owner, c, i.direct_dependencies_exact.mpr source, exec⟩

theorem direct_tool_need_is_needed (i : ToolInput) {g : Symbol} {e : Config}
    (h : i.DirectToolNeed g e) : Needed i g e := by
  obtain ⟨owner, c, dep, exec⟩ := h
  exact Needed.tool dep exec

/-- No global "needed iff run" claim: other needed sources remain visible. -/
theorem needed_sources (i : ToolInput) {g : Symbol} {e : Config}
    (h : Needed i g e) : i.otherNeeded g e ∨ i.DirectToolNeed g e := by
  cases h with
  | other h => exact Or.inl h
  | tool dep exec => exact Or.inr ⟨_, _, dep, exec⟩

/- These eliminators establish the least-model interpretation, including
the positive Needed/Dependency recursion. A cyclic proof cannot bootstrap
either relation without a finite constructor derivation. -/
mutual
  theorem needed_minimal (i : ToolInput)
      (N : Symbol → Config → Prop) (D : Symbol → Config → Symbol → Prop)
      (seed : ∀ t c, i.otherNeeded t c → N t c)
      (ref : ∀ t c a r use g, Ref i t c a r use → Resolves i t r g → D t c g)
      (built : ∀ t c tc role g, N t c → i.targetToolchain t tc →
        i.tc.toolTarget tc role g → D t c g)
      (tool : ∀ t c g e, D t c g → ConfigInput.Exec i.tc.config c e → N g e)
      {t : Symbol} {c : Config} (h : Needed i t c) : N t c :=
    match h with
    | .other hs => seed _ _ hs
    | .tool dep exec =>
      tool _ _ _ _ (dependency_minimal i N D seed ref built tool dep) exec

  theorem dependency_minimal (i : ToolInput)
      (N : Symbol → Config → Prop) (D : Symbol → Config → Symbol → Prop)
      (seed : ∀ t c, i.otherNeeded t c → N t c)
      (ref : ∀ t c a r use g, Ref i t c a r use → Resolves i t r g → D t c g)
      (built : ∀ t c tc role g, N t c → i.targetToolchain t tc →
        i.tc.toolTarget tc role g → D t c g)
      (tool : ∀ t c g e, D t c g → ConfigInput.Exec i.tc.config c e → N g e)
      {t g : Symbol} {c : Config} (h : Dependency i t c g) : D t c g :=
    match h with
    | .reference hr resolved => ref _ _ _ _ _ _ hr resolved
    | .builtToolchain need chosen program =>
      built _ _ _ _ _ (needed_minimal i N D seed ref built tool need) chosen program
end

/-- Without any ordinary root or typed reference, the positive built-toolchain
cycle cannot create the first needed fact. This depends on leastness, not
merely satisfaction of the recursive clauses. -/
theorem no_roots_no_needed (i : ToolInput)
    (noSeeds : ∀ t c, ¬ i.otherNeeded t c)
    (noRefs : ∀ t c a r use, ¬ Ref i t c a r use) :
    ∀ t c, ¬ Needed i t c := by
  intro t c need
  exact i.needed_minimal (fun _ _ => False) (fun _ _ _ => False)
    (fun t c seed => noSeeds t c seed)
    (fun t c a r use _ ref _ => noRefs t c a r use ref)
    (fun _ _ _ _ _ false _ _ => false)
    (fun _ _ _ _ false _ => false) need

theorem name_candidates_end_at_targets (i : ToolInput) {r g : Symbol}
    (h : NameCandidate i r g) : i.targetId g := by
  induction h with
  | target target => exact target
  | alias _ _ ih => exact ih

theorem typed_scalar_needed (i : ToolInput) {a pre r use t g : Symbol}
    {rank : Int} {c e : Config}
    (run : i.actionRun a rank pre r use) (owner : i.tc.targetAction t c a)
    (resolved : Resolves i t r g) (exec : ConfigInput.Exec i.tc.config c e) :
    Needed i g e :=
  Needed.tool (Dependency.reference (Ref.scalar run owner) resolved) exec

theorem typed_list_item_needed (i : ToolInput) {a r use t g : Symbol}
    {rank index : Int} {c e : Config}
    (run : i.actionRunItem a rank index r use) (owner : i.tc.targetAction t c a)
    (resolved : Resolves i t r g) (exec : ConfigInput.Exec i.tc.config c e) :
    Needed i g e :=
  Needed.tool (Dependency.reference (Ref.item run owner) resolved) exec

theorem built_toolchain_tools_needed (i : ToolInput) {t tc role g : Symbol} {c e : Config}
    (need : Needed i t c) (chosen : i.targetToolchain t tc)
    (program : i.tc.toolTarget tc role g) (exec : ConfigInput.Exec i.tc.config c e) :
    Needed i g e :=
  Needed.tool (Dependency.builtToolchain need chosen program) exec

/-- The paper's forward invariant, with exec(c,e) explicit rather than an
assumed total function `tool(c)`. Its host and uniqueness properties are
proved in Configuration.lean, conditional on the shipped checks as needed. -/
theorem tool_configuration_invariant (i : ToolInput) {t a r use g : Symbol} {c e : Config}
    (ref : Ref i t c a r use) (resolved : Resolves i t r g)
    (exec : ConfigInput.Exec i.tc.config c e) :
    Needed i g e ∧ i.tc.config.hostPlatform e.platform :=
  ⟨Needed.tool (Dependency.reference ref resolved) exec, i.tc.config.exec_targets_host exec⟩

theorem typed_reference_unique_host_instance (i : ToolInput)
    (clean : ¬ ConfigInput.Diagnostic i.tc.config)
    (named : ∀ c, ConfigInput.Known i.tc.config c → ∃ n, i.tc.config.cfgName c n)
    (uniqueHost : ∀ a b, i.tc.config.hostPlatform a → i.tc.config.hostPlatform b → a = b)
    {host t a r use g : Symbol} {c e : Config}
    (hostPresent : i.tc.config.hostPlatform host)
    (ref : Ref i t c a r use) (resolved : Resolves i t r g)
    (exec : ConfigInput.Exec i.tc.config c e) :
    Needed i g e ∧ e.platform = host ∧
      ∀ other, ConfigInput.Exec i.tc.config c other → other = e :=
  ⟨Needed.tool (Dependency.reference ref resolved) exec,
    i.tc.config.exec_targets_the_host uniqueHost hostPresent exec,
    fun _ h => i.tc.config.exec_config_functional clean named h exec⟩

-- The multi-head path rules are represented by their shared derivation;
-- each simultaneously states an argv word/part and an implicit input.
inductive ScalarPath (i : ToolInput) : Symbol → Int → Symbol → Symbol → Prop
  | expand : i.actionRun a rank pre r use → i.tc.targetAction t c a →
      Resolves i t r g → ConfigInput.Exec i.tc.config c e → i.runArtifact g e path →
      ScalarPath i a rank pre path

inductive ListPath (i : ToolInput) : Symbol → Int → Int → Symbol → Prop
  | expand : i.actionRunItem a rank index r use → i.tc.targetAction t c a →
      Resolves i t r g → ConfigInput.Exec i.tc.config c e → i.runArtifact g e path →
      ListPath i a rank index path

inductive BuiltPath (i : ToolInput) : Symbol → Symbol → Prop
  | expand {tc : Symbol} : i.actionTcTool a tc role → i.tc.toolTarget tc role g →
      i.tc.targetAction t c a → ConfigInput.Exec i.tc.config c e → i.runArtifact g e path →
      BuiltPath i a path

def ScalarImplicitInput (i : ToolInput) (a path : Symbol) : Prop :=
  ∃ rank pre, ScalarPath i a rank pre path
def ListImplicitInput (i : ToolInput) (a path : Symbol) : Prop :=
  ∃ rank index, ListPath i a rank index path

theorem scalar_path_needs_host (i : ToolInput) {a pre path : Symbol} {rank : Int}
    (h : ScalarPath i a rank pre path) :
    i.ScalarImplicitInput a path ∧
      ∃ g e, i.runArtifact g e path ∧ Needed i g e ∧ i.tc.config.hostPlatform e.platform := by
  cases h with
  | expand run owner resolved exec artifact =>
    exact ⟨⟨_, _, ScalarPath.expand run owner resolved exec artifact⟩,
      _, _, artifact, i.typed_scalar_needed run owner resolved exec,
      i.tc.config.exec_targets_host exec⟩

theorem list_path_needs_host (i : ToolInput) {a path : Symbol} {rank index : Int}
    (h : ListPath i a rank index path) :
    i.ListImplicitInput a path ∧
      ∃ g e, i.runArtifact g e path ∧ Needed i g e ∧ i.tc.config.hostPlatform e.platform := by
  cases h with
  | expand run owner resolved exec artifact =>
    exact ⟨⟨_, _, ListPath.expand run owner resolved exec artifact⟩,
      _, _, artifact, i.typed_list_item_needed run owner resolved exec,
      i.tc.config.exec_targets_host exec⟩

/-- The built-role rendering clause alone guarantees host configuration.
Need comes from the separate owner/target_toolchain association above,
not from an arbitrary action_tc_tool fact supplied by an extension. -/
theorem built_path_uses_host (i : ToolInput) {a path : Symbol} (h : BuiltPath i a path) :
    ∃ g e, i.runArtifact g e path ∧ i.tc.config.hostPlatform e.platform := by
  cases h with
  | expand _ _ _ exec artifact =>
    exact ⟨_, _, artifact, i.tc.config.exec_targets_host exec⟩

inductive HasArtifact (i : ToolInput) : Symbol → Config → Prop
  | artifact : i.runArtifact g e path → HasArtifact i g e

inductive Unrunnable (i : ToolInput) : Symbol → Config → Symbol → Config → Prop
  | tool : Dependency i t c g → ConfigInput.Exec i.tc.config c e →
      ¬ HasArtifact i g e → i.tc.config.cfgName e name → Unrunnable i t c g e

theorem missing_tool_artifact_error (i : ToolInput) {t g : Symbol} {c e : Config}
    (dep : Dependency i t c g) (exec : ConfigInput.Exec i.tc.config c e)
    (missing : ∀ path, ¬ i.runArtifact g e path)
    (named : ∃ name, i.tc.config.cfgName e name) : Unrunnable i t c g e := by
  obtain ⟨name, hn⟩ := named
  apply Unrunnable.tool dep exec _ hn
  intro has
  cases has with
  | artifact h => exact missing _ h

end ToolInput
end Buildlog.Core
