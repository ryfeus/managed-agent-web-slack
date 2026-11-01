import { build } from "esbuild";
import { mkdir, writeFile, rm } from "node:fs/promises";
import { execFileSync } from "node:child_process";

if (Number(process.versions.node.split(".")[0]) !== 24) throw new Error("Build Lambda functions with Node 24");
for (const name of ["web-function", "cma-preview", "slack-preview"]) {
  const directory = `dist/${name}`;
  await rm(directory, { recursive: true, force: true });
  await mkdir(directory, { recursive: true });
  await build({ entryPoints: [`apps/${name}/src/index.ts`], outfile: `${directory}/index.js`,
    bundle: true, platform: "node", target: "node24", format: "esm", sourcemap: false,
    // Some official dependencies still use require for Node builtins.
    banner: { js: 'import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);' },
    external: ["bufferutil", "utf-8-validate"], logLevel: "info" });
  await writeFile(`${directory}/package.json`, JSON.stringify({ type: "module", engines: { node: "24.x" } }));
  execFileSync(process.execPath, ["--check", `${directory}/index.js`], { stdio: "inherit" });
  await mkdir("dist/lambdas", { recursive: true });
  // Fixed timestamps and file order make S3 revisions depend on content only.
  execFileSync("python3", ["-c", `import pathlib, zipfile
root=pathlib.Path('${directory}')
with zipfile.ZipFile('dist/lambdas/${name}.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
 for path in sorted(root.iterdir()):
  info=zipfile.ZipInfo(path.name, (2026,1,1,0,0,0)); info.external_attr=0o100644 << 16; info.compress_type=zipfile.ZIP_DEFLATED
  archive.writestr(info, path.read_bytes())`], { stdio: "inherit" });
}
execFileSync("bash", ["scripts/validate_lambda_artifacts.sh", "--functions"], { stdio: "inherit" });
