# 🚀 AWS Lambda Deployer com SAM

[![Dr. Leslie](https://img.shields.io/badge/Desenvolvido%20por-Dr.%20Leslie-blue?style=for-the-badge)](https://github.com/leslieclemence)
[![I Love Python](https://img.shields.io/badge/I%20%E2%9D%A4%EF%B8%8F-Python-yellow?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![AWS Lambda](https://img.shields.io/badge/AWS-Lambda-orange?style=for-the-badge&logo=amazon-aws&logoColor=white)](https://aws.amazon.com/lambda/)
[![SAM](https://img.shields.io/badge/AWS-SAM-orange?style=for-the-badge&logo=amazon-aws&logoColor=white)](https://aws.amazon.com/serverless/sam/)

Script interativo em Python para criar, atualizar e fazer deploy de funções AWS Lambda utilizando o AWS SAM (Serverless Application Model).

---

## 📋 Índice

- [Recursos](#-recursos)
- [Pré-requisitos](#-pré-requisitos)
- [Instalação](#-instalação)
- [Como Usar](#-como-usar)
- [Funcionalidades Detalhadas](#-funcionalidades-detalhadas)
- [Estrutura de Arquivos](#-estrutura-de-arquivos)
- [Exemplos](#-exemplos)
- [Troubleshooting](#-troubleshooting)
- [Contribuição](#-contribuição)
- [Licença](#-licença)

---

## ✨ Recursos

- 🎯 **Deploy interativo** - Interface amigável via terminal
- 🔍 **Detecção automática** - Encontra funções Lambda existentes na AWS
- 📦 **Gerenciamento de roles IAM** - Cria ou reutiliza roles de execução
- 🌐 **Function URLs** - Configura URLs públicas com CORS
- 🔗 **URLs Personalizadas (Custom Domains)** - Criação e vinculação automática com CloudFront, ACM (SSL/TLS) e Route 53
- 💾 **Configuração persistente** - Salva configurações para deploys futuros
- 🔧 **Variáveis de ambiente** - Suporte completo a environment variables
- 📚 **Layers** - Adicione layers às suas funções
- 🏗️ **Múltiplas arquiteturas** - Suporte a x86_64 e ARM64 (Graviton2)

---

## 📌 Pré-requisitos

### 1. Python 3.8+

```bash
python --version
```

### 2. AWS SAM CLI

**macOS (Homebrew):**
```bash
brew install aws-sam-cli
```

**Linux:**
```bash
pip install aws-sam-cli
```

**Windows:**
```bash
choco install aws-sam-cli
```

Verifique a instalação:
```bash
sam --version
```

### 3. AWS CLI configurado

```bash
aws configure
```

Você precisará de:
- AWS Access Key ID
- AWS Secret Access Key
- Região padrão (ex: `sa-east-1`)

---

## 🔧 Instalação

### 1. Clone o repositório

```bash
git clone https://github.com/camedics/lambda_deployer.git
cd lambda_deployer
```

### 2. Crie um ambiente virtual (recomendado)

```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
# ou
.\venv\Scripts\activate  # Windows
```

### 3. Instale as dependências

```bash
pip install -r requirements.txt
```

---

## 🚀 Como Usar

### Uso Básico

```bash
python lambda_deployer.py
```

O script irá guiá-lo através de um processo interativo:

1. **Perfil AWS** - Selecione um perfil existente por número ou nome
2. **Nome da função** - Digite o nome da sua Lambda
3. **Arquivo do código** - Selecione o arquivo .py com seu handler
4. **Função handler** - Escolha qual função será o entry point
5. **Runtime** - Selecione a versão do Python (ou outra linguagem)
6. **Configurações** - Memória, timeout, arquitetura
7. **Role IAM** - Crie ou selecione uma role de execução
8. **Deploy** - Execute `sam build` e `sam deploy`

### Perfil AWS

Ao iniciar, o deployer lista os perfis encontrados pelo AWS CLI ou nos arquivos
`~/.aws/credentials` e `~/.aws/config`. Você pode selecionar pelo número exibido
ou digitar o nome do perfil. O perfil escolhido é salvo em
`.lambda_deployer_config.json` e também é usado pelos comandos `sam build`,
`sam deploy` e `sam delete`.

### Fluxo Rápido (com configuração salva)

Se você já executou o deployer antes, ele irá:

1. Detectar a configuração salva
2. Perguntar se deseja reutilizá-la
3. Executar o build e deploy automaticamente

---

## 📚 Funcionalidades Detalhadas

### 🔐 Gerenciamento de Roles IAM

O deployer oferece várias opções para roles de execução:

- **Usar role existente** - Lista roles compatíveis com Lambda
- **Criar role básica** - Cria role com permissões mínimas (CloudWatch Logs)
- **Criar role customizada** - A partir de arquivo JSON ou inline
- **Informar ARN manualmente** - Para roles já existentes

### 🌐 Function URLs

Configure URLs públicas para suas Lambdas:

```
🌐 Deseja expor a função com URL pública? [s/N]: s
   Permitir acesso sem autenticação? [S/n]: s
   Configurar CORS? [s/N]: s
   Origens permitidas (separadas por vírgula) [*]: https://meusite.com
   Métodos permitidos (separados por vírgula) [*]: GET,POST
```

### 🔗 URLs Personalizadas (Custom Domains)

O deployer permite expor a Function URL sob um domínio/subdomínio customizado de forma totalmente automatizada:
1. **SSL/TLS com ACM (Amazon Certificate Manager)**: Solicita ou reutiliza um certificado na região `us-east-1` (exigido pelo CloudFront).
2. **Validação DNS**: Cria automaticamente o registro CNAME de validação do certificado no Route 53 e aguarda a emissão.
3. **Distribuição CDN com CloudFront**: Configura a distribuição apontando para a Function URL como origem, passando cabeçalhos e métodos HTTP corretos.
4. **Registros DNS no Route 53**: Cria ou atualiza os registros de Alias A e AAAA para apontar o domínio customizado para o endereço do CloudFront.

Ao iniciar o deployer, se a função possuir uma URL pública ativada, ele perguntará se você deseja configurar ou atualizar o link HTTPS personalizado.

### 🔧 Variáveis de Ambiente

```
🔧 Deseja adicionar variáveis de ambiente? [s/N]: s
   Digite as variáveis no formato CHAVE=VALOR (linha vazia para finalizar)
   Variável: DATABASE_URL=postgres://...
   Variável: API_KEY=secret123
   Variável: 
```

### 📦 Layers

```
📦 Deseja adicionar Layers? [s/N]: s
   Digite os ARNs dos layers (linha vazia para finalizar)
   Layer ARN: arn:aws:lambda:sa-east-1:123456789:layer:minha-layer:1
   Layer ARN: 
```

---

## 📁 Estrutura de Arquivos

Após executar o deployer, seu projeto terá a seguinte estrutura:

```
meu-projeto/
├── lambda_deployer.py          # Script do deployer
├── requirements.txt            # Dependências do deployer
├── minha_funcao.py             # Seu código Lambda
├── template.yaml               # Template SAM (gerado)
├── samconfig.toml              # Configuração do SAM (gerado)
├── .lambda_deployer_config.json # Config salva (gerado)
├── .gitignore                  # Git ignore (gerado)
├── .samignore                  # SAM ignore (gerado)
└── .aws-sam/                   # Build artifacts (gerado)
```

---

## 💡 Exemplos

### Exemplo de Handler Lambda

```python
# minha_funcao.py

def lambda_handler(event, context):
    """
    Handler principal da Lambda
    
    Processa requisições HTTP e retorna uma resposta
    """
    name = event.get('queryStringParameters', {}).get('name', 'Mundo')
    
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps({
            'message': f'Olá, {name}!',
            'timestamp': datetime.now().isoformat()
        })
    }
```

### Exemplo de Política IAM Customizada

Crie um arquivo `iam_policy.json`:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject"
            ],
            "Resource": "arn:aws:s3:::meu-bucket/*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:Query"
            ],
            "Resource": "arn:aws:dynamodb:*:*:table/MinhaTabela"
        }
    ]
}
```

---

## 🔧 Comandos SAM Úteis

Após o deploy, você pode usar estes comandos:

```bash
# Construir a aplicação
sam build

# Deploy (usa samconfig.toml)
sam deploy

# Deploy guiado (interativo)
sam deploy --guided

# Ver logs da função
sam logs -n MinhaFuncao --tail

# Invocar localmente
sam local invoke MinhaFuncao -e event.json

# API local para testes
sam local start-api

# Deletar stack
sam delete
```

---

## 🐛 Troubleshooting

### Erro: "No changes to deploy"

A stack já está atualizada. Faça alterações no código antes de fazer deploy novamente.

### Erro: Stack em estado de ROLLBACK

```
⚠ A stack está em um estado de erro/rollback
🗑️  Deseja deletar a stack e tentar novamente? [S/n]: s
```

O deployer oferece a opção de deletar a stack automaticamente.

### Erro: Role não encontrada

Verifique se:
1. A role existe no IAM
2. A role tem a trust policy para `lambda.amazonaws.com`
3. Você tem permissões para assumir a role

### Erro: SAM CLI não instalado

```bash
# macOS
brew install aws-sam-cli

# Linux/Windows
pip install aws-sam-cli
```

---

## 🤝 Contribuição

Contribuições são bem-vindas! Por favor:

1. Faça um Fork do projeto
2. Crie uma branch para sua feature (`git checkout -b feature/AmazingFeature`)
3. Commit suas mudanças (`git commit -m 'Add some AmazingFeature'`)
4. Push para a branch (`git push origin feature/AmazingFeature`)
5. Abra um Pull Request

---

## 📄 Licença

Este projeto está sob a licença MIT. Veja o arquivo [LICENSE](LICENSE) para mais detalhes.

---

## ❤️ Feito com amor por Dr. Leslie

[![Dr. Leslie](https://img.shields.io/badge/Made%20with%20%E2%9D%A4%EF%B8%8F%20by-Dr.%20Leslie-blue?style=flat-square)](https://github.com/leslieclemence)
[![Python](https://img.shields.io/badge/Powered%20by-Python-yellow?style=flat-square&logo=python&logoColor=white)](https://python.org)

---

**⭐ Se este projeto te ajudou, deixe uma estrela!**
