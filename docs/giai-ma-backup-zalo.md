# Decrypting a Zalo Desktop backup (.zl.zip)

Recorded: 2026-10-15. Use this only on a backup of your own account.

## Scope and verified result

A Zalo backup is an encrypted stream, even though the file name ends in `.zl.zip`. Once decrypted, the content is
a TAR archive (ustar header), not a ZIP.

The method below was verified on a backup of about 1 GB from Zalo PC 26.10.11. The TAR held one folder and two
JSON Lines files:

- `<UID>_zmessage.zdb`: the messages.
- `<UID>_zconversation.zdb`: conversation metadata.

The export parsed without a single malformed JSON line. The original file was left untouched. The UIN is never
written into documents; the temporary UIN file is deleted when done.

## 1. Get the UIN from the logged-in Zalo session

The UIN is the account's internal 32-character hex id; it is not the phone number. The backup key is derived
from it.

With Zalo PC logged in, temporarily open the Chrome DevTools Protocol on loopback only:

    open -a /Applications/Zalo.app --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=9229

Check the local endpoint:

    curl http://127.0.0.1:9229/json/list

Use the Zalo page's `webSocketDebuggerUrl` to call `Runtime.evaluate`. In the version checked, the React tree is
under:

    document.querySelector("#app")._reactRootContainer._internalRoot.current

Walk the fibers through `child` and `sibling`, checking `memoizedProps`, `pendingProps`, `memoizedState`,
`updateQueue` and `dependencies`. In that session the UIN was at `memoizedProps.target.UIN`. Internal structure
can change between versions; only accept a 32-character hex UIN and confirm it with the TAR header in step 3.

Never print the UIN to a terminal or a chat. Write it to a temporary file with mode 600 and read it only to
decrypt. The debugger port gives access to the Zalo session while it is open; never bind it to 0.0.0.0 or any
other network address.

After getting the UIN, quit Zalo to close the listener, and confirm the port is closed:

    lsof -nP -iTCP:9229 -sTCP:LISTEN

## 2. Derive the key and decrypt

The verified scheme:

- Key: SHA-256 of the UIN string encoded as UTF-8 (32 bytes).
- IV: the UTF-8 string `"zie"` followed by the first 13 characters of the UIN (16 bytes in total).
- Cipher: AES-256-CBC with default PKCS#7 padding.
- Plaintext: TAR.

Streaming decryption in Node.js. Keep the backup path, the temporary UIN file and the output TAR in local
variables; the `wx` flag refuses to overwrite an existing file:

    import fs from "node:fs";
    import crypto from "node:crypto";
    import { pipeline } from "node:stream/promises";

    const uin = fs.readFileSync("/private/path/uin.private.txt", "utf8").trim();
    if (!/^[0-9a-f]{32}$/i.test(uin)) throw new Error("invalid UIN");

    const key = crypto.createHash("sha256").update(uin, "utf8").digest();
    const iv = Buffer.from("zie" + uin.slice(0, 13), "utf8");
    const decipher = crypto.createDecipheriv("aes-256-cbc", key, iv);

    await pipeline(
      fs.createReadStream("/path/to/backup.zl.zip"),
      decipher,
      fs.createWriteStream("/path/to/decrypted.tar", { flags: "wx", mode: 0o600 })
    );

Streaming avoids loading the whole backup into memory. The decrypted TAR can be almost as large as the backup;
make sure there is enough disk space for it and for the files you extract.

## 3. Confirm the key before extracting

Decrypt only the first 512 bytes with AES-CBC, with auto-padding turned off. Check that:

1. Bytes 257–261 are the string `ustar`.
2. The checksum at bytes 148–155 (octal) equals the sum of the 512 header bytes, with bytes 148–155 replaced by
   ASCII spaces.

If the magic or the checksum does not match, stop: the UIN, the format version or the algorithm assumption may be
wrong. Never extract unconfirmed data.

## 4. Extract safely and read the content

Open the TAR with Python's `tarfile` in streaming mode `r|*`. Before writing any member, reject absolute paths,
`..` components, backslashes, symlinks, hard links, devices and anything that is not a regular file. Write only
the files you need into a new folder with mode 700 (files 600); never use `extractall` on an unchecked archive.

The `*_zmessage.zdb` and `*_zconversation.zdb` files are JSON Lines: one JSON object per line, so they can be read
line by line with `json.loads` to keep memory low. Useful fields:

- `msgId`, `sendDttm`, `fromUid`, `toUid`, `dName`, `msgType`.
- `message`, `content`, `quote` and `attach`.

`sendDttm` is a millisecond timestamp. Keep `msgId` and the content fields as they are to avoid losing data. Some
records carry the sender or conversation id instead of a display name. To load the messages into kim-rag, see
"Loading older messages from a Zalo PC backup" in the README (`zalo/import.js`).

## 5. Protecting the data

- Only process backups of accounts you are entitled to access; never upload the backup, the UIN or message
  contents to an outside service.
- Keep the original backup read-only; decrypt and export into a new folder, outside any git repository.
- Delete the temporary UIN file after verification and close the debugger port.
- The decrypted content is private data. Text/JSONL exports can contain every message; restrict file and folder
  permissions where you keep them.
- The React structure, the backup algorithm and the JSON schema can change with Zalo versions; always confirm the
  TAR and check the schema before exporting large amounts of data.
