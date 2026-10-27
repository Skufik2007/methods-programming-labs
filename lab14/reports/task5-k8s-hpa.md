# Задание 5. Развёртывание в Kubernetes с автоскалированием

**Требование:** упаковать Go-сборщик в Docker-образ, развернуть конвейер в minikube или
k3s, настроить HPA для сборщика по длине очереди или загрузке CPU.

## Что сделано

- **Образы.**
  - [`deploy/go.Dockerfile`](../deploy/go.Dockerfile) — многоэтапная сборка, alpine,
    пользователь 10001, оба бинарника (эмулятор и сборщик).
  - [`deploy/python.Dockerfile`](../deploy/python.Dockerfile) — первый этап собирает
    Rust-колесо maturin, итоговый образ без компиляторов.
- **Манифесты** [`k8s/`](../k8s) (kustomize):
  - namespace, etcd, NATS, эмулятор;
  - сборщик — Deployment с `resources.requests.cpu`, readiness по `/status`, initContainer
    ждёт зависимости, `COLLECTOR_ID` = имя пода;
  - [`hpa.yaml`](../k8s/hpa.yaml) — `autoscaling/v2`, CPU 60%, 1–4 реплики.
- **Связь с заданием 1.** Новым репликам не нужна настройка. Под регистрируется в etcd,
  и HRW перераспределяет на него часть устройств. HPA добавляет сборщики, а работа делится
  сама.
- **Кластер.** На машине разработки нет Docker (так же, как в ЛР №11 и №13), поэтому
  кластер поднимается в CI: задача `k8s` в [`ci.yml`](../.github/workflows/ci.yml) создаёт
  **kind** (Kubernetes в Docker) — это аналог minikube и k3s для CI. Дальше по шагам:
  1. установить metrics-server;
  2. загрузить образы;
  3. `kubectl apply -k k8s/`;
  4. повысить нагрузку на эмуляторе (`DEVICES=3000`, `PERIOD_MS=50`);
  5. ждать, пока HPA поднимет реплики (таймаут 6 мин). Вывод `kubectl get hpa,pods`
     сохраняется в артефакт.

## Результат (CI, первый же запуск)

```
=== 12:33:49 readyReplicas=1 ===
horizontalpodautoscaler.autoscaling/collector   Deployment/collector   cpu: <unknown>/60%   1   4   1   44s
=== 12:33:55 readyReplicas=3 ===
horizontalpodautoscaler.autoscaling/collector   Deployment/collector   cpu: 985%/60%        1   4   1   50s
```

После роста нагрузки загрузка CPU сборщика составила 985% от запроса при цели 60%, и
HPA поднял Deployment до **3 готовых реплик**. Задача проверяет именно число реплик больше
1, а не сам факт успешного `apply`.

## Ограничение

minikube и k3s локально не запускались (нет Docker). Проверка воспроизводима в любом
запуске CI, лог HPA — в артефакте `k8s-out`.
