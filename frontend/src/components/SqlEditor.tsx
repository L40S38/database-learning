import { sql, PostgreSQL } from "@codemirror/lang-sql";
import { oneDark } from "@codemirror/theme-one-dark";
import CodeMirror from "@uiw/react-codemirror";

interface Props {
  value: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  height?: string;
}

export default function SqlEditor({ value, onChange, readOnly = false, height = "160px" }: Props) {
  return (
    <CodeMirror
      value={value}
      height={height}
      theme={oneDark}
      extensions={[sql({ dialect: PostgreSQL })]}
      onChange={onChange}
      readOnly={readOnly}
      editable={!readOnly}
      basicSetup={{ lineNumbers: true, foldGutter: false, highlightActiveLine: !readOnly, highlightActiveLineGutter: !readOnly }}
    />
  );
}
