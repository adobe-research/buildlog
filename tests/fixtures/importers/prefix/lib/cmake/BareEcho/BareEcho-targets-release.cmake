#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "echo" for configuration "Release"
set_property(TARGET echo APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(echo PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "CXX"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libecho.a"
  )

list(APPEND _cmake_import_check_targets echo )
list(APPEND _cmake_import_check_files_for_echo "${_IMPORT_PREFIX}/lib/libecho.a" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
