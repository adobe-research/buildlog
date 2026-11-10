#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "GreetAbs::greet" for configuration "Release"
set_property(TARGET GreetAbs::greet APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(GreetAbs::greet PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "CXX"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libgreet.a"
  )

list(APPEND _cmake_import_check_targets GreetAbs::greet )
list(APPEND _cmake_import_check_files_for_GreetAbs::greet "${_IMPORT_PREFIX}/lib/libgreet.a" )

# Import target "GreetAbs::shout" for configuration "Release"
set_property(TARGET GreetAbs::shout APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(GreetAbs::shout PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libshout.1.2.3.dylib"
  IMPORTED_SONAME_RELEASE "@rpath/libshout.1.dylib"
  )

list(APPEND _cmake_import_check_targets GreetAbs::shout )
list(APPEND _cmake_import_check_files_for_GreetAbs::shout "${_IMPORT_PREFIX}/lib/libshout.1.2.3.dylib" )

# Import target "GreetAbs::greet-tool" for configuration "Release"
set_property(TARGET GreetAbs::greet-tool APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(GreetAbs::greet-tool PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/bin/greet-tool"
  )

list(APPEND _cmake_import_check_targets GreetAbs::greet-tool )
list(APPEND _cmake_import_check_files_for_GreetAbs::greet-tool "${_IMPORT_PREFIX}/bin/greet-tool" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
