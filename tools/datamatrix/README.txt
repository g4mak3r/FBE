FBE DataMatrix runtime

- datamatrix-1.5.jar: bundled GS1 DataMatrix runtime (artifact metadata: gs1:datamatrix:1.5; Barcode4J 2.1).
- fbe-datamatrix-helper.jar: FBE adapter that reads the exact KIZ from a UTF-8 file and invokes the renderer.
- RenderOne.java: source of the FBE adapter.

The KIZ is never passed as a command-line string. This preserves ASCII 29 GS separators and avoids shell escaping.

Third-party license/NOTICE files are retained in the project root under third_party/ and THIRD_PARTY_NOTICES.md.
