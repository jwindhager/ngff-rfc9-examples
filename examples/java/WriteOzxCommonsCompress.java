import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;

import org.apache.commons.compress.archivers.zip.Zip64Mode;
import org.apache.commons.compress.archivers.zip.ZipArchiveEntry;
import org.apache.commons.compress.archivers.zip.ZipArchiveOutputStream;

/**
 * Pack an OME-Zarr directory into an OME-Zarr zip file (.ozx) with Apache Commons Compress
 * (org.apache.commons:commons-compress, tested with 1.27.1; it also needs commons-io and
 * commons-lang3 at run time).
 *
 * Compile and run (Java 11+), with the jars in lib/:
 *   javac -cp 'lib/*' WriteOzxCommonsCompress.java
 *   java  -cp 'lib/*:.' WriteOzxCommonsCompress path/to/image.ome.zarr out.ozx
 *
 * Compared to java.util.zip this library can
 *  - force ZIP64 for every entry (setUseZip64(Zip64Mode.Always)),  SHOULD
 *  - write STORED entries to a file without knowing size/CRC in advance (it seeks
 *    back to patch the headers); this needs a File or SeekableByteChannel target.
 * It still writes the central directory in the order the entries were written.
 */
public class WriteOzxCommonsCompress {
    static int depth(String name) {
        int d = 0;
        for (char c : name.toCharArray()) if (c == '/') d++;
        return d;
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 2) {
            System.err.println("usage: java WriteOzxCommonsCompress <ome.zarr directory> <out.ozx>");
            System.exit(2);
        }
        Path root = Paths.get(args[0]);
        List<Path> files = new ArrayList<>();
        try (Stream<Path> s = Files.walk(root)) {
            s.filter(Files::isRegularFile).forEach(files::add);
        }
        Comparator<Path> order = Comparator
            .comparing((Path p) -> !p.getFileName().toString().equals("zarr.json"))
            .thenComparingInt(p -> depth(root.relativize(p).toString().replace('\\', '/')))
            .thenComparing(Path::toString);
        files.sort(order);

        String comment = "{\"ome\":{\"version\":\"0.5\",\"zipFile\":{\"centralDirectory\":{\"jsonFirst\":true}}}}";

        try (ZipArchiveOutputStream zos = new ZipArchiveOutputStream(new File(args[1]))) {
            zos.setMethod(ZipEntry.STORED);            // MUST: no ZIP-level compression
            zos.setUseZip64(Zip64Mode.Always);         // SHOULD: ZIP64 for every entry
            zos.setComment(comment);
            for (Path p : files) {
                String name = root.relativize(p).toString().replace('\\', '/');
                ZipArchiveEntry e = new ZipArchiveEntry(name);
                e.setMethod(ZipEntry.STORED);
                e.setSize(Files.size(p));              // required for STORED unless the target is seekable
                zos.putArchiveEntry(e);
                Files.copy(p, zos);
                zos.closeArchiveEntry();
            }
        }
    }
}
