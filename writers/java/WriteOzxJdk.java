import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Stream;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/**
 * Pack an OME-Zarr directory into an OME-Zarr zip file (.ozx) using only the JDK.
 *
 * Run (Java 11+): java WriteOzxJdk.java path/to/image.ome.zarr out.ozx
 *
 * RFC-9 requirements addressed:
 *  - every entry is STORED                    (MUST)   ZipEntry.STORED needs size and CRC up front
 *  - no extra fields                          (MUST)   none are written for entries without
 *                                                      explicit access/creation times
 *  - archive comment with the ome JSON        (SHOULD) ZipOutputStream.setComment
 *  - zarr.json records first, breadth-first   (SHOULD) see limitation below
 *
 * Limitations of java.util.zip (see the guidance text of the RFC):
 *  - ZipOutputStream writes the central directory in the order the entries were
 *    written, so the zarr.json-first order of the central directory forces all
 *    zarr.json entries to be written (and therefore known) before any chunk.
 *  - ZIP64 is used automatically only when required (more than 65535 entries or
 *    sizes/offsets beyond 4 GiB); there is no API to request it for small archives.
 */
public class WriteOzxJdk {
    static int depth(String name) {
        int d = 0;
        for (char c : name.toCharArray()) if (c == '/') d++;
        return d;
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 2) {
            System.err.println("usage: java WriteOzxJdk.java <ome.zarr directory> <out.ozx>");
            System.exit(2);
        }
        Path root = Paths.get(args[0]);
        List<Path> files = new ArrayList<>();
        try (Stream<Path> s = Files.walk(root)) {
            s.filter(Files::isRegularFile).forEach(files::add);
        }
        // zarr.json first (root first, then by depth = breadth-first), then everything else
        Comparator<Path> order = Comparator
            .comparing((Path p) -> !p.getFileName().toString().equals("zarr.json"))
            .thenComparingInt(p -> depth(root.relativize(p).toString().replace('\\', '/')))
            .thenComparing(Path::toString);
        files.sort(order);

        String comment = "{\"ome\":{\"version\":\"0.5\",\"zipFile\":{\"centralDirectory\":{\"jsonFirst\":true}}}}";

        try (OutputStream out = Files.newOutputStream(Paths.get(args[1]));
             ZipOutputStream zos = new ZipOutputStream(out)) {
            zos.setMethod(ZipOutputStream.STORED);
            zos.setComment(comment);  // UTF-8 JSON; at most 65535 bytes
            for (Path p : files) {
                String name = root.relativize(p).toString().replace('\\', '/');
                // STORED entries need size and CRC-32 before the entry is written
                CRC32 crc = new CRC32();
                long size = 0;
                try (InputStream in = Files.newInputStream(p)) {
                    byte[] buf = new byte[1 << 20];
                    for (int n; (n = in.read(buf)) > 0; ) { crc.update(buf, 0, n); size += n; }
                }
                ZipEntry e = new ZipEntry(name);
                e.setMethod(ZipEntry.STORED);
                e.setSize(size);
                e.setCompressedSize(size);
                e.setCrc(crc.getValue());
                e.setTime(Files.getLastModifiedTime(p).toMillis());  // DOS time only: no extra field
                zos.putNextEntry(e);
                Files.copy(p, zos);
                zos.closeEntry();
            }
        }
    }
}
