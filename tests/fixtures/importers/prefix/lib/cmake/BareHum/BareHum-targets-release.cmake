#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "hum" for configuration "Release"
set_property(TARGET hum APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(hum PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "CXX"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libhum.a"
  )

list(APPEND _cmake_import_check_targets hum )
list(APPEND _cmake_import_check_files_for_hum "${_IMPORT_PREFIX}/lib/libhum.a" )

# Import target "hum_dylib" for configuration "Release"
set_property(TARGET hum_dylib APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(hum_dylib PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libhum_dylib.dylib"
  IMPORTED_SONAME_RELEASE "@rpath/libhum_dylib.dylib"
  )

list(APPEND _cmake_import_check_targets hum_dylib )
list(APPEND _cmake_import_check_files_for_hum_dylib "${_IMPORT_PREFIX}/lib/libhum_dylib.dylib" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
