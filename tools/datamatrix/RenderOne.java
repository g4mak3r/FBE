package fbe.datamatrix;

import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import gs1.datamatrix.DatamatrixRenderer;

public final class RenderOne {
    private RenderOne() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 3) {
            System.err.println("Usage: RenderOne <input-cis-file> <output-png> <dpi>");
            System.exit(2);
        }
        byte[] bytes = Files.readAllBytes(Paths.get(args[0]));
        String cis = new String(bytes, StandardCharsets.UTF_8);
        int dpi = Integer.parseInt(args[2]);
        try (FileOutputStream out = new FileOutputStream(args[1])) {
            DatamatrixRenderer.render2Png(cis, dpi, out);
        }
    }
}
