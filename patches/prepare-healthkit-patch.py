from pathlib import Path
p = Path(__file__).with_name("apply-healthkit-training-signals.py")
s = p.read_text()
slash = chr(92)
s = s.replace(slash * 2 + "n", slash + "n")
s = s.replace(slash * 2 + "(", slash + "(")
p.write_text(s)
print("Normalized patch-script escapes.")
