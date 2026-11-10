# SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Generic probe of a CMake find module (M3 phase 1 assessment, option 1).
#
# Runs one find_package call and dumps what it produced: every target
# reachable from the root targets over INTERFACE_LINK_LIBRARIES and
# IMPORTED_LINK_DEPENDENT_LIBRARIES_<CFG>, with its raw properties (generator
# expressions unevaluated), plus the variables matching PROBE_VAR_REGEX. A
# file(GENERATE) companion records the build-type-evaluated view.
#
#   cmake -S llvm-probe -B <build> -G Ninja -DCMAKE_BUILD_TYPE=Release
#         -DPROBE_MODULE_PATH=<dir> -DPROBE_PACKAGE=<pkg> -DPROBE_ARGS=<args;...>
#         -DPROBE_ROOT_REGEX=<regex> -DPROBE_ROOTS=<t;...> -DPROBE_VAR_REGEX=<regex>
#         -DPROBE_OUT=<dir> [-D<Pkg>_ROOT=...] [-DPROBE_ALL=ON]
#
# Nothing here is specific to a package; the Halide registration is the
# argument set in inputs/*.json (FindHalide_LLVM with Halide's
# CMakeLists.txt:194 arguments).
cmake_minimum_required(VERSION 3.28)
project(find_module_probe LANGUAGES C CXX)

foreach (v IN ITEMS PROBE_PACKAGE PROBE_OUT)
    if (NOT ${v})
        message(FATAL_ERROR "set ${v}")
    endif ()
endforeach ()
list(PREPEND CMAKE_MODULE_PATH ${PROBE_MODULE_PATH})

string(TIMESTAMP _t0 "%s%f")
find_package(${PROBE_PACKAGE} ${PROBE_ARGS})
string(TIMESTAMP _t1 "%s%f")
math(EXPR _find_us "${_t1} - ${_t0}")

# ---- JSON helpers -----------------------------------------------------------

function(_json_str out s)
    string(REPLACE "\\" "\\\\" s "${s}")
    string(REPLACE "\"" "\\\"" s "${s}")
    string(REPLACE "\n" "\\n" s "${s}")
    string(REPLACE "\t" "\\t" s "${s}")
    set(${out} "\"${s}\"" PARENT_SCOPE)
endfunction()

# ---- property names -----------------------------------------------------------
# CMake cannot list a target's properties, so the candidate names are CMake's
# documented INTERFACE_*/IMPORTED* properties plus every INTERFACE_<X> token in
# the package files that were read (custom ones such as INTERFACE_CXX_RTTI).

execute_process(COMMAND "${CMAKE_COMMAND}" --help-property-list OUTPUT_VARIABLE _plist)
string(REPLACE "\n" ";" _plist "${_plist}")
set(_iface "")
set(_imported "")
foreach (p IN LISTS _plist)
    if (p MATCHES "^INTERFACE_[A-Z_]+$")
        list(APPEND _iface "${p}")
    elseif (p MATCHES "^(IMPORTED[A-Z_]*)(_<CONFIG>)?$")
        list(APPEND _imported "${CMAKE_MATCH_1}")
    endif ()
endforeach ()
set(_dirs ${PROBE_MODULE_PATH})
get_cmake_property(_all_vars VARIABLES)
foreach (v IN LISTS _all_vars)
    if (v MATCHES "_DIR$" AND NOT v MATCHES "^(CMAKE|_)" AND EXISTS "${${v}}/")
        list(APPEND _dirs "${${v}}")
    endif ()
endforeach ()
foreach (dir IN LISTS _dirs)
    file(GLOB _files "${dir}/*.cmake")
    foreach (f IN LISTS _files)
        file(STRINGS "${f}" _hits REGEX "INTERFACE_[A-Z_]+")
        foreach (h IN LISTS _hits)
            string(REGEX MATCHALL "INTERFACE_[A-Z_]+" _m "${h}")
            list(APPEND _iface ${_m})
        endforeach ()
    endforeach ()
endforeach ()
list(REMOVE_DUPLICATES _iface)
list(SORT _iface)
list(REMOVE_DUPLICATES _imported)
list(SORT _imported)

# ---- reachability -------------------------------------------------------------

get_property(_all DIRECTORY PROPERTY IMPORTED_TARGETS)
set(_roots "")
foreach (t IN LISTS _all)
    if (PROBE_ROOT_REGEX AND t MATCHES "${PROBE_ROOT_REGEX}")
        list(APPEND _roots "${t}")
    endif ()
endforeach ()
foreach (t IN LISTS PROBE_ROOTS)
    if (TARGET "${t}")
        list(APPEND _roots "${t}")
    endif ()
endforeach ()
# PROBE_ALL dumps every imported target (for the genex census), not just the closure.
if (PROBE_ALL)
    set(_roots ${_all})
endif ()

# Target names inside one link item, generator expressions stripped.
function(_link_names out item)
    set(names "")
    if (item MATCHES "^::@")
        set(${out} "" PARENT_SCOPE)
        return()
    endif ()
    if (item MATCHES "\\$<")
        string(REGEX REPLACE "\\$<[A-Za-z_]+:" ";" s "${item}")
        string(REGEX REPLACE "[>,]" ";" s "${s}")
        foreach (x IN LISTS s)
            if (x AND TARGET "${x}")
                list(APPEND names "${x}")
            endif ()
        endforeach ()
    elseif (TARGET "${item}")
        set(names "${item}")
    endif ()
    set(${out} "${names}" PARENT_SCOPE)
endfunction()

set(_seen "")
set(_queue ${_roots})
set(_edges "")
set(_esep "")
while (_queue)
    list(POP_FRONT _queue t)
    if (t IN_LIST _seen)
        continue()
    endif ()
    list(APPEND _seen "${t}")
    get_target_property(_cfgs "${t}" IMPORTED_CONFIGURATIONS)
    set(_lists "INTERFACE_LINK_LIBRARIES")
    if (_cfgs)
        foreach (c IN LISTS _cfgs)
            list(APPEND _lists "IMPORTED_LINK_DEPENDENT_LIBRARIES_${c}")
        endforeach ()
    endif ()
    foreach (lp IN LISTS _lists)
        get_target_property(_v "${t}" "${lp}")
        if (NOT _v)
            continue()
        endif ()
        foreach (item IN LISTS _v)
            _link_names(_n "${item}")
            foreach (n IN LISTS _n)
                _json_str(_a "${t}")
                _json_str(_b "${n}")
                _json_str(_k "${lp}")
                _json_str(_i "${item}")
                string(APPEND _edges "${_esep}{\"from\":${_a},\"to\":${_b},\"via\":${_k},\"item\":${_i}}")
                set(_esep ",\n")
                list(APPEND _queue "${n}")
            endforeach ()
        endforeach ()
    endforeach ()
endwhile ()
list(SORT _seen)

# ---- dump -------------------------------------------------------------------------

set(_tjson "")
set(_tsep "")
set(_gen "")
foreach (t IN LISTS _seen)
    get_target_property(_type "${t}" TYPE)
    get_target_property(_imp "${t}" IMPORTED)
    get_target_property(_cfgs "${t}" IMPORTED_CONFIGURATIONS)
    set(_names ${_iface})
    foreach (b IN LISTS _imported)
        list(APPEND _names "${b}")
        if (_cfgs)
            foreach (c IN LISTS _cfgs)
                list(APPEND _names "${b}_${c}")
            endforeach ()
        endif ()
    endforeach ()
    # COMPATIBLE_INTERFACE_* names further INTERFACE_<X> properties.
    foreach (cp IN ITEMS COMPATIBLE_INTERFACE_BOOL COMPATIBLE_INTERFACE_STRING
            COMPATIBLE_INTERFACE_NUMBER_MIN COMPATIBLE_INTERFACE_NUMBER_MAX)
        get_target_property(_cv "${t}" "${cp}")
        if (_cv)
            list(APPEND _names "${cp}")
            foreach (x IN LISTS _cv)
                list(APPEND _names "INTERFACE_${x}")
            endforeach ()
        endif ()
    endforeach ()
    list(REMOVE_DUPLICATES _names)
    set(_props "")
    set(_psep "")
    set(_has_genex false)
    foreach (p IN LISTS _names)
        get_property(_set TARGET "${t}" PROPERTY "${p}" SET)
        if (NOT _set)
            continue()
        endif ()
        get_property(_v TARGET "${t}" PROPERTY "${p}")
        if (_v MATCHES "\\$<")
            set(_has_genex true)
        endif ()
        _json_str(_k "${p}")
        _json_str(_s "${_v}")
        string(APPEND _props "${_psep}${_k}:${_s}")
        set(_psep ",")
    endforeach ()
    _json_str(_n "${t}")
    _json_str(_ty "${_type}")
    set(_isimp false)
    if (_imp)
        set(_isimp true)
    endif ()
    string(APPEND _tjson "${_tsep}"
        "${_n}:{\"type\":${_ty},\"imported\":${_isimp},\"has_genex\":${_has_genex},\"props\":{${_props}}}")
    set(_tsep ",\n")
    # Evaluated companion: location and transitive usage requirements.
    set(_file "")
    if (NOT _type STREQUAL "INTERFACE_LIBRARY")
        set(_file "$<TARGET_FILE:${t}>")
    endif ()
    string(APPEND _gen "${t}\tfile\t${_file}\n")
    foreach (p IN ITEMS INTERFACE_COMPILE_DEFINITIONS INTERFACE_INCLUDE_DIRECTORIES
            INTERFACE_COMPILE_OPTIONS INTERFACE_LINK_OPTIONS)
        string(APPEND _gen "${t}\t${p}\t$<JOIN:$<TARGET_PROPERTY:${t},${p}>,|>\n")
    endforeach ()
endforeach ()

get_cmake_property(_vars VARIABLES)
list(SORT _vars)
set(_vjson "")
set(_vsep "")
foreach (v IN LISTS _vars)
    if (PROBE_VAR_REGEX AND v MATCHES "${PROBE_VAR_REGEX}")
        _json_str(_k "${v}")
        _json_str(_s "${${v}}")
        string(APPEND _vjson "${_vsep}${_k}:${_s}")
        set(_vsep ",\n")
    endif ()
endforeach ()

set(_imp_json "")
foreach (t IN LISTS _all)
    _json_str(_s "${t}")
    list(APPEND _imp_json "${_s}")
endforeach ()
list(JOIN _imp_json "," _imp_json)
set(_root_json "")
foreach (t IN LISTS _roots)
    _json_str(_s "${t}")
    list(APPEND _root_json "${_s}")
endforeach ()
list(JOIN _root_json "," _root_json)

file(MAKE_DIRECTORY "${PROBE_OUT}")
file(WRITE "${PROBE_OUT}/dump.json" "{
\"cmake_version\": \"${CMAKE_VERSION}\",
\"find_package_us\": ${_find_us},
\"roots\": [${_root_json}],
\"imported_targets\": [${_imp_json}],
\"edges\": [
${_edges}
],
\"targets\": {
${_tjson}
},
\"variables\": {
${_vjson}
}
}
")
file(GENERATE OUTPUT "${PROBE_OUT}/evaluated-$<CONFIG>.tsv" CONTENT "${_gen}")
message(STATUS "probe: ${PROBE_OUT}/dump.json (${_find_us} us in find_package)")
