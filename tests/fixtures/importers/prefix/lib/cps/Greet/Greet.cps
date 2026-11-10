{
  "components" : 
  {
    "greet" : 
    {
      "compile_features" : [ "c++17" ],
      "definitions" : 
      {
        "*" : 
        {
          "GREET_API" : "1"
        }
      },
      "includes" : [ "@prefix@/include" ],
      "link_requires" : [ ":greet_detail" ],
      "type" : "archive"
    },
    "greet-tool" : 
    {
      "type" : "executable"
    },
    "greet_detail" : 
    {
      "definitions" : 
      {
        "*" : 
        {
          "GREET_DETAIL" : "1"
        }
      },
      "type" : "interface"
    },
    "shout" : 
    {
      "requires" : [ ":greet" ],
      "type" : "dylib"
    }
  },
  "configurations" : [ "Release", "Debug" ],
  "cps_path" : "@prefix@/lib/cps/Greet",
  "cps_version" : "0.14.1",
  "default_components" : [ "greet" ],
  "name" : "Greet",
  "version" : "1.2.3"
}