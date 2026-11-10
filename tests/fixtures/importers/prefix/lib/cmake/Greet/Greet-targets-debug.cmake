#----------------------------------------------------------------
# Generated CMake target import file for configuration "Debug".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "Greet::greet" for configuration "Debug"
set_property(TARGET Greet::greet APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)
set_target_properties(Greet::greet PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_DEBUG "CXX"
  IMPORTED_LOCATION_DEBUG "${_IMPORT_PREFIX}/lib/libgreetd.a"
  )

list(APPEND _cmake_import_check_targets Greet::greet )
list(APPEND _cmake_import_check_files_for_Greet::greet "${_IMPORT_PREFIX}/lib/libgreetd.a" )

# Import target "Greet::shout" for configuration "Debug"
set_property(TARGET Greet::shout APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)
set_target_properties(Greet::shout PROPERTIES
  IMPORTED_LOCATION_DEBUG "${_IMPORT_PREFIX}/lib/libshoutd.1.2.3.dylib"
  IMPORTED_SONAME_DEBUG "@rpath/libshoutd.1.dylib"
  )

list(APPEND _cmake_import_check_targets Greet::shout )
list(APPEND _cmake_import_check_files_for_Greet::shout "${_IMPORT_PREFIX}/lib/libshoutd.1.2.3.dylib" )

# Import target "Greet::greet-tool" for configuration "Debug"
set_property(TARGET Greet::greet-tool APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)
set_target_properties(Greet::greet-tool PROPERTIES
  IMPORTED_LOCATION_DEBUG "${_IMPORT_PREFIX}/bin/greet-tool"
  )

list(APPEND _cmake_import_check_targets Greet::greet-tool )
list(APPEND _cmake_import_check_files_for_Greet::greet-tool "${_IMPORT_PREFIX}/bin/greet-tool" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
