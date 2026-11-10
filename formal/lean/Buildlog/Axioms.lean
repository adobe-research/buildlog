-- SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

import Buildlog.Closure
import Buildlog.Preparation
import Buildlog.Consequences
import Buildlog.Dependency
import Buildlog.Offers
import Buildlog.Examples
import Buildlog.CacheConfirmation
import Buildlog.CacheSeeding
import Buildlog.CacheExamples
import Buildlog.CompletionGate
import Buildlog.GateExamples

-- These commands run as part of a clean build, auditing all headline results.
#print axioms Buildlog.Facts.finite_progress
#print axioms Buildlog.bounded_progress
#print axioms Buildlog.Positive.fixed
#print axioms Buildlog.Positive.least
#print axioms Buildlog.Stratified.finite_rounds
#print axioms Buildlog.Stratified.inert_answers
#print axioms Buildlog.ground_inert_answers
#print axioms Buildlog.GroundStratum.guarded_operator
#print axioms Buildlog.Stratified.closed_exists
#print axioms Buildlog.Stratified.closed_models
#print axioms Buildlog.Stratified.least_closed_guarded_answers
#print axioms Buildlog.Stratified.least_unique
#print axioms Buildlog.Stratified.round_model
#print axioms Buildlog.Preparation.terminates
#print axioms Buildlog.Preparation.closed
#print axioms Buildlog.Preparation.least
#print axioms Buildlog.Stratified.fixed_union_closure
#print axioms Buildlog.Driver.all_guarded_batch
#print axioms Buildlog.CachePhase.fixed_union_agreement
#print axioms Buildlog.Driver.fresh_termination
#print axioms Buildlog.Driver.first_stopping_round
#print axioms Buildlog.Driver.fresh_determinism
#print axioms Buildlog.Driver.probe_order_independent
#print axioms Buildlog.Driver.guarded_first
#print axioms Buildlog.Driver.stopping_round_deterministic
#print axioms Buildlog.Driver.outcome_deterministic
#print axioms Buildlog.Restarts.restarts_terminate
#print axioms Buildlog.Restarts.restart_determinism
#print axioms Buildlog.Restarts.least_restart_roots
#print axioms Buildlog.Restarts.grouping_independent
#print axioms Buildlog.Restarts.grouping_terminates
#print axioms Buildlog.stutter_cannot_repeat
#print axioms Buildlog.offer_loop_terminates
#print axioms Buildlog.Protected.explicit_target_requests_answer_free
#print axioms Buildlog.IndependentSlice.models_agree
#print axioms Buildlog.Driver.errors_do_not_steer_discovery
#print axioms Buildlog.Stratified.final_errors_agree
#print axioms Buildlog.Examples.request_operator_not_monotone
#print axioms Buildlog.Examples.unguarded_has_no_least_closed
#print axioms Buildlog.CacheConfirmation.active_answers_correct
#print axioms Buildlog.BundleProgram.model_flatten
#print axioms Buildlog.CachePhase.active_rows_correct
#print axioms Buildlog.CachePhase.reduced
#print axioms Buildlog.BundleProgram.package_termination
#print axioms Buildlog.CachePhaseRun.follows_schedule
#print axioms Buildlog.BundleProgram.completed_phase_run
#print axioms Buildlog.CompletedConfigure.canonical
#print axioms Buildlog.CompletedConfigure.cached_fresh_agree
#print axioms Buildlog.Examples.cache_hit_matches_cache_free
#print axioms Buildlog.Examples.replay_matches_cache_free
#print axioms Buildlog.Examples.stale_hit_violates_FC
#print axioms Buildlog.GroundProgram.model_source
#print axioms Buildlog.GroundProgram.lower_rank_stable
#print axioms Buildlog.CompletionProgram.Isolation.models_agree
#print axioms Buildlog.CompletionProgram.Isolation.discovery_agrees
#print axioms Buildlog.CompletionProgram.Isolation.halt_agrees
#print axioms Buildlog.CompletionProgram.Definition.no_pending
#print axioms Buildlog.CompletionProgram.Definition.complete_holds
#print axioms Buildlog.CompletionProgram.Definition.final_models_agree
#print axioms Buildlog.CompletionProgram.Isolation.prop_errors
#print axioms Buildlog.Examples.gate_completion_derived
#print axioms Buildlog.Examples.gate_final_errors_agree
#print axioms Buildlog.Examples.gate_discovery_agrees
