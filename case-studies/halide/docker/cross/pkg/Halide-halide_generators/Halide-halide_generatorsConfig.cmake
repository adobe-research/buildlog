# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Shadow Halide-halide_generators package for the macOS -> Linux Halide cross build
# (M3 phase 7c). Used with -DHalide-halide_generators_DIR=<this directory> and
# -DHALIDE_CROSS_HOST_BUILD=<the macOS host build tree>.
#
# Halide's supported cross flow imports the host build's generators
# (-DHalide-halide_generators_ROOT=<host build>). That works until a generator is handed
# an autoscheduler plugin: add_halide_library(... AUTOSCHEDULER Halide::Adams2019) passes
# the *target* build's plugin (a Linux .so) to the macOS generator's -p, and dlopen fails.
# CMake does not apply CROSSCOMPILING_EMULATOR to IMPORTED executables, so each imported
# generator's location is replaced by a wrapper script that runs it through the same
# host-twin.py, which maps -p plugins to their host-build twins.
if (NOT HALIDE_CROSS_HOST_BUILD)
    message(FATAL_ERROR "Set HALIDE_CROSS_HOST_BUILD to the host build tree")
endif ()

get_directory_property(_hcg_before IMPORTED_TARGETS)
include("${HALIDE_CROSS_HOST_BUILD}/cmake/Halide-halide_generatorsConfig.cmake")
get_directory_property(_hcg_after IMPORTED_TARGETS)
list(REMOVE_ITEM _hcg_after ${_hcg_before})

string(REPLACE ";" "' '" _hcg_emulator "'${CMAKE_CROSSCOMPILING_EMULATOR}'")
foreach (_hcg_target IN LISTS _hcg_after)
    get_target_property(_hcg_type "${_hcg_target}" TYPE)
    if (NOT _hcg_type STREQUAL "EXECUTABLE")
        continue()
    endif ()
    get_target_property(_hcg_configs "${_hcg_target}" IMPORTED_CONFIGURATIONS)
    foreach (_hcg_config IN LISTS _hcg_configs)
        get_target_property(_hcg_loc "${_hcg_target}" IMPORTED_LOCATION_${_hcg_config})
        get_filename_component(_hcg_name "${_hcg_loc}" NAME)
        set(_hcg_wrapper "${CMAKE_BINARY_DIR}/host-twin-wrappers/${_hcg_name}")
        file(
            CONFIGURE OUTPUT "${_hcg_wrapper}"
            CONTENT "#!/bin/sh\nexec ${_hcg_emulator} '${_hcg_loc}' \"$@\"\n"
        )
        file(CHMOD "${_hcg_wrapper}" PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ
             GROUP_EXECUTE WORLD_READ WORLD_EXECUTE)
        set_target_properties("${_hcg_target}" PROPERTIES IMPORTED_LOCATION_${_hcg_config} "${_hcg_wrapper}")
    endforeach ()
endforeach ()
