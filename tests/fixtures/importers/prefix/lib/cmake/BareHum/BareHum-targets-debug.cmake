#----------------------------------------------------------------
# Generated CMake target import file for configuration "Debug".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "hum" for configuration "Debug"
set_property(TARGET hum APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)
set_target_properties(hum PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_DEBUG "CXX"
  IMPORTED_LOCATION_DEBUG "${_IMPORT_PREFIX}/lib/libhumd.a"
  )

list(APPEND _cmake_import_check_targets hum )
list(APPEND _cmake_import_check_files_for_hum "${_IMPORT_PREFIX}/lib/libhumd.a" )

# Import target "hum_dylib" for configuration "Debug"
set_property(TARGET hum_dylib APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)
set_target_properties(hum_dylib PROPERTIES
  IMPORTED_LOCATION_DEBUG "${_IMPORT_PREFIX}/lib/libhum_dylibd.dylib"
  IMPORTED_SONAME_DEBUG "@rpath/libhum_dylibd.dylib"
  )

list(APPEND _cmake_import_check_targets hum_dylib )
list(APPEND _cmake_import_check_files_for_hum_dylib "${_IMPORT_PREFIX}/lib/libhum_dylibd.dylib" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
