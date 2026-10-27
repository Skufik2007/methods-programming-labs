# Микробенчмарки валидации (Windows 11, 12 логических ядер)

## Go: go test -bench . -benchmem

```
goos: windows
cpu: AMD Ryzen 5 5600H with Radeon Graphics         
BenchmarkValidate-12    	  353745	      3395 ns/op	     227 B/op	       6 allocs/op
goos: windows
cpu: AMD Ryzen 5 5600H with Radeon Graphics         
BenchmarkPing-12             	  126660	      8098 ns/op	    7228 B/op	      40 allocs/op
BenchmarkValidateOrder-12    	   56320	     22582 ns/op	    9559 B/op	      71 allocs/op
```

## Python: bench/micro_validate.py (Pydantic)

```
model_validate(dict)          11.94 мкс/операция   пик памяти   2400 Б/операция
model_validate_json(str)      11.40 мкс/операция   пик памяти   2461 Б/операция
```
