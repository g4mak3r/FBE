# Third-party notices

FBE includes or interoperates with third-party components. Their licenses are independent from the FBE source-code notice in `LICENSE`.

## GS1 DataMatrix runtime / Barcode4J

Bundled file:

```text
tools/datamatrix/datamatrix-1.5.jar
```

Metadata embedded in the JAR identifies:

- artifact: `gs1:datamatrix:1.5`;
- implementation: `GS1 DataMatrix Library`;
- bundled Barcode4J version: `2.1.0`;
- dependency: `net.sf.barcode4j:barcode4j:2.1`.

The JAR itself contains an Apache License 2.0 text and Barcode4J NOTICE. Exact copies extracted from the distributed binary are retained as:

- [`third_party/Apache-2.0-Barcode4J.txt`](third_party/Apache-2.0-Barcode4J.txt)
- [`third_party/NOTICE-Barcode4J.txt`](third_party/NOTICE-Barcode4J.txt)

The small FBE Java adapter is distributed with its source:

```text
tools/datamatrix/RenderOne.java
tools/datamatrix/fbe-datamatrix-helper.jar
```

FBE does not claim ownership of Barcode4J or other third-party code contained in the bundled DataMatrix runtime.

## Python dependencies

Python packages listed in `requirements.txt` and `requirements-dev.txt` are installed from their respective package sources and remain governed by their own licenses. They are not vendored into this repository.

## Local commercial software

FBE can integrate with locally installed tools such as BarTender. Such software is not included in this repository and remains governed by its vendor license.
