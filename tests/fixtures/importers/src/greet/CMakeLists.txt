# Fixture for buildlog-cpp's CMake export and CPS importers. Regenerate the
# checked-in outputs with tests/fixtures/importers/regenerate.sh; never edit
# them by hand.
cmake_minimum_required(VERSION 4.0)
project(Greet VERSION 1.2.3 LANGUAGES CXX)

set(CMAKE_DEBUG_POSTFIX d)

# Link-only helper of a static library: exported as $<LINK_ONLY:...>.
add_library(greet_detail INTERFACE)
target_compile_definitions(greet_detail INTERFACE GREET_DETAIL=1)

add_library(greet STATIC src/greet.cpp)
target_sources(greet PUBLIC FILE_SET HEADERS BASE_DIRS include FILES include/greet/greet.h)
target_compile_features(greet PUBLIC cxx_std_17)
target_compile_definitions(greet PUBLIC GREET_API=1)
target_link_libraries(greet PRIVATE greet_detail)

add_library(shout SHARED src/shout.cpp)
target_link_libraries(shout PUBLIC greet)
set_target_properties(shout PROPERTIES VERSION 1.2.3 SOVERSION 1)

add_executable(greet-tool src/tool.cpp)
target_link_libraries(greet-tool PRIVATE greet)

# Every generator expression the importer supports, as CMake exports them.
# CPS forbids generator expressions, so extras is in its own export set; it
# links whisper from a third set (the export's missing-targets check).
add_library(whisper STATIC src/whisper.cpp)
add_library(Greet::whisper ALIAS whisper)
add_library(extras INTERFACE)
target_compile_definitions(extras INTERFACE
  "$<$<NOT:$<TARGET_EXISTS:PNG::PNG>>:EXTRAS_NO_PNG>"
  "$<$<PLATFORM_ID:Darwin>:EXTRAS_MACOS>"
  "$<$<PLATFORM_ID:AIX>:EXTRAS_AIX>"
  "$<$<COMPILE_LANGUAGE:CXX>:EXTRAS_CXX>"
  "$<BUILD_INTERFACE:EXTRAS_BUILD_TREE>"
  "$<INSTALL_INTERFACE:EXTRAS_INSTALLED>"
  "$<$<BOOL:1>:EXTRAS_ON>"
  "$<$<CONFIG:Debug>:EXTRAS_DEBUG>")
target_link_libraries(extras INTERFACE
  whisper
  "$<TARGET_NAME_IF_EXISTS:PNG::PNG>"
  "$<LINK_LIBRARY:WHOLE_ARCHIVE,Greet::whisper>"
  "$<$<CONFIG:Release>:-Wl,-dead_strip>")

set(targets greet_detail greet shout greet-tool)

# Relocatable install: _IMPORT_PREFIX is computed from the file's location.
install(TARGETS ${targets} EXPORT greet-targets
  ARCHIVE DESTINATION lib LIBRARY DESTINATION lib RUNTIME DESTINATION bin
  FILE_SET HEADERS DESTINATION include)
install(EXPORT greet-targets NAMESPACE Greet:: DESTINATION lib/cmake/Greet
  FILE Greet-targets.cmake)
# Absolute destination: the prefix is fixed at configure time.
install(EXPORT greet-targets NAMESPACE GreetAbs:: DESTINATION ${CMAKE_INSTALL_PREFIX}/lib/cmake/GreetAbs
  FILE GreetAbs-targets.cmake)
# CPS, next to the CMake files.
install(PACKAGE_INFO Greet EXPORT greet-targets DESTINATION lib/cps/Greet
  DEFAULT_TARGETS greet DEFAULT_CONFIGURATIONS Release Debug)

install(TARGETS whisper EXPORT whisper-targets ARCHIVE DESTINATION lib)
install(EXPORT whisper-targets NAMESPACE Greet:: DESTINATION lib/cmake/GreetWhisper
  FILE GreetWhisper-targets.cmake)
install(TARGETS extras EXPORT extras-targets)
install(EXPORT extras-targets NAMESPACE Greet:: DESTINATION lib/cmake/GreetExtras
  FILE GreetExtras-targets.cmake)

# Build tree: configurations inline, absolute paths, no _IMPORT_PREFIX.
export(EXPORT greet-targets NAMESPACE GreetBuild:: FILE GreetBuild-targets.cmake)
export(EXPORT whisper-targets NAMESPACE GreetBuild:: FILE GreetBuildWhisper-targets.cmake)
export(EXPORT extras-targets NAMESPACE GreetBuild:: FILE GreetBuildExtras-targets.cmake)

# Un-namespaced exports, as LLVM ships them (`LLVMSupport`, not
# `LLVM::LLVMSupport`), for the importer's per-import namespace option.
# BareEcho links hum from BareHum: the export's missing-targets check names
# it bare.
add_library(hum STATIC src/hum.cpp)
target_compile_definitions(hum INTERFACE HUM=1)
add_library(hum_dylib SHARED src/hum.cpp)
target_link_libraries(hum_dylib PRIVATE hum)
add_library(echo STATIC src/echo.cpp)
target_link_libraries(echo PUBLIC hum PRIVATE hum_dylib)
install(TARGETS hum hum_dylib EXPORT hum-targets ARCHIVE DESTINATION lib LIBRARY DESTINATION lib)
install(TARGETS echo EXPORT echo-targets ARCHIVE DESTINATION lib)
install(EXPORT hum-targets DESTINATION lib/cmake/BareHum FILE BareHum-targets.cmake)
install(EXPORT echo-targets DESTINATION lib/cmake/BareEcho FILE BareEcho-targets.cmake)
