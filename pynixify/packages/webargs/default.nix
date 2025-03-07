# WARNING: This file was automatically generated. You should avoid editing it.
# If you run pynixify again, the file will be either overwritten or
# deleted, and you will lose the changes you made to it.

{ buildPythonPackage
, fetchPypi
, lib
, marshmallow
, packaging
}:

buildPythonPackage rec {
  pname =
    "webargs";
  version =
    "8.1.0";

  src =
    fetchPypi {
      inherit
        pname
        version;
      sha256 =
        "0csxdbxdk25d4ga1gi52m5ys973h4q4zqa85fp7n68m2akqbgw7i";
    };

  propagatedBuildInputs =
    [
      marshmallow
      packaging
    ];

  # TODO FIXME
  doCheck =
    false;

  meta =
    with lib; {
      description =
        "Declarative parsing and validation of HTTP request objects, with built-in support for popular web frameworks, including Flask, Django, Bottle, Tornado, Pyramid, Falcon, and aiohttp.";
      homepage =
        "https://github.com/marshmallow-code/webargs";
    };
}
