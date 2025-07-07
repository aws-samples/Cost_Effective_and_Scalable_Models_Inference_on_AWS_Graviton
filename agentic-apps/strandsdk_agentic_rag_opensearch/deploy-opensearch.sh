#!/bin/bash

# Deploy OpenSearch CloudFormation Stack with EKS Pod Identity mapping
# Usage: ./deploy-opensearch.sh [stack-name] [region] [namespace]

set -e

STACK_NAME=${1:-"strandsdk-rag-opensearch-stack"}
REGION=${2:-"us-east-1"}
NAMESPACE=${3:-"default"}
TEMPLATE_FILE="opensearch-cluster-simple.yaml"

echo "🚀 Deploying OpenSearch CloudFormation Stack with EKS Pod Identity..."
echo "Stack Name: $STACK_NAME"
echo "Region: $REGION"
echo "Namespace: $NAMESPACE"
echo "Template: $TEMPLATE_FILE"
echo ""

# Get EKS cluster name from current kubeconfig context
echo "🔍 Reading EKS cluster name from kubeconfig..."
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl is not installed or not in PATH"
    exit 1
fi

CURRENT_CONTEXT=$(kubectl config current-context 2>/dev/null || echo "")
if [ -z "$CURRENT_CONTEXT" ]; then
    echo "❌ No current kubectl context found"
    echo "Please set your kubectl context to point to your EKS cluster"
    exit 1
fi

# Extract cluster name from context (format: arn:aws:eks:region:account:cluster/cluster-name)
EKS_CLUSTER_NAME=$(echo $CURRENT_CONTEXT | sed 's/.*cluster\///')
if [ -z "$EKS_CLUSTER_NAME" ]; then
    echo "❌ Could not extract EKS cluster name from context: $CURRENT_CONTEXT"
    exit 1
fi

echo "✅ Found EKS cluster: $EKS_CLUSTER_NAME"
echo ""

# Check if AWS CLI is configured
if ! aws sts get-caller-identity > /dev/null 2>&1; then
    echo "❌ AWS CLI is not configured or credentials are invalid"
    echo "Please run 'aws configure' first"
    exit 1
fi

# Check if template file exists
if [ ! -f "$TEMPLATE_FILE" ]; then
    echo "❌ Template file $TEMPLATE_FILE not found"
    exit 1
fi

# Check if EKS cluster exists
echo "🔍 Verifying EKS cluster exists..."
if ! aws eks describe-cluster --name $EKS_CLUSTER_NAME --region $REGION > /dev/null 2>&1; then
    echo "❌ EKS cluster $EKS_CLUSTER_NAME not found in region $REGION"
    echo "Please ensure the EKS cluster exists and your kubeconfig is correct"
    exit 1
fi

# Check if EKS cluster has Pod Identity addon enabled
echo "🔍 Checking EKS Pod Identity addon..."
POD_IDENTITY_STATUS=$(aws eks describe-addon \
    --cluster-name $EKS_CLUSTER_NAME \
    --addon-name eks-pod-identity-agent \
    --region $REGION \
    --query 'addon.status' \
    --output text 2>/dev/null || echo "NOT_FOUND")

if [ "$POD_IDENTITY_STATUS" != "ACTIVE" ]; then
    echo "⚠️  EKS Pod Identity addon is not active on cluster $EKS_CLUSTER_NAME"
    echo "Installing EKS Pod Identity addon..."
    aws eks create-addon \
        --cluster-name $EKS_CLUSTER_NAME \
        --addon-name eks-pod-identity-agent \
        --region $REGION \
        --resolve-conflicts OVERWRITE
    
    echo "⏳ Waiting for Pod Identity addon to become active..."
    aws eks wait addon-active \
        --cluster-name $EKS_CLUSTER_NAME \
        --addon-name eks-pod-identity-agent \
        --region $REGION
    echo "✅ Pod Identity addon is now active"
fi

# Validate the template
echo "🔍 Validating CloudFormation template..."
aws cloudformation validate-template \
    --template-body file://$TEMPLATE_FILE \
    --region $REGION

if [ $? -eq 0 ]; then
    echo "✅ Template validation successful"
else
    echo "❌ Template validation failed"
    exit 1
fi

# Deploy the stack
echo ""
echo "📦 Deploying CloudFormation stack..."
aws cloudformation deploy \
    --template-file $TEMPLATE_FILE \
    --stack-name $STACK_NAME \
    --region $REGION \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides \
        ClusterName=strandsdk-rag-opensearch \
        ServiceAccountName=strandsdk-rag-service-account \
        EKSClusterName=$EKS_CLUSTER_NAME \
        KubernetesNamespace=$NAMESPACE

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Stack deployment successful!"
    echo ""
    echo "📋 Getting stack outputs..."
    aws cloudformation describe-stacks \
        --stack-name $STACK_NAME \
        --region $REGION \
        --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
        --output table
    
    echo ""
    echo "🔐 To get the master password, run:"
    echo "aws secretsmanager get-secret-value --secret-id strandsdk-rag-opensearch-master-password --region $REGION --query SecretString --output text | jq -r .password"
    
    echo ""
    echo "🌐 OpenSearch Dashboards will be available at:"
    ENDPOINT=$(aws cloudformation describe-stacks \
        --stack-name $STACK_NAME \
        --region $REGION \
        --query 'Stacks[0].Outputs[?OutputKey==`OpenSearchDomainEndpoint`].OutputValue' \
        --output text)
    echo "https://$ENDPOINT/_dashboards"
    
    echo ""
    echo "🔗 EKS Pod Identity Association created:"
    echo "Cluster: $EKS_CLUSTER_NAME"
    echo "Namespace: $NAMESPACE"
    echo "Service Account: strandsdk-rag-service-account"
    
    echo ""
    echo "⏳ Waiting for OpenSearch cluster to be ready..."
    echo "This may take 15-20 minutes..."
    
    # Wait for OpenSearch cluster to be ready
    CLUSTER_STATUS=""
    WAIT_COUNT=0
    MAX_WAIT=60  # 60 * 30 seconds = 30 minutes max wait
    
    while [ "$CLUSTER_STATUS" != "Active" ] && [ $WAIT_COUNT -lt $MAX_WAIT ]; do
        sleep 30
        CLUSTER_STATUS=$(aws opensearch describe-domain \
            --domain-name strandsdk-rag-opensearch \
            --region $REGION \
            --query 'DomainStatus.Processing' \
            --output text 2>/dev/null)
        
        if [ "$CLUSTER_STATUS" = "False" ]; then
            CLUSTER_STATUS="Active"
        fi
        
        WAIT_COUNT=$((WAIT_COUNT + 1))
        echo "   Waiting... ($WAIT_COUNT/60)"
    done
    
    if [ "$CLUSTER_STATUS" = "Active" ]; then
        echo "✅ OpenSearch cluster is ready!"
        
        # Setup OpenSearch index
        echo ""
        echo "🔧 Setting up OpenSearch index..."
        
        # Check if setup_opensearch_index.py exists
        if [ -f "setup_opensearch_index.py" ]; then
            # Get the service account role ARN from CloudFormation output
            SERVICE_ACCOUNT_ROLE_ARN=$(aws cloudformation describe-stacks \
                --stack-name $STACK_NAME \
                --region $REGION \
                --query 'Stacks[0].Outputs[?OutputKey==`ServiceAccountRoleArn`].OutputValue' \
                --output text)
            
            # Set environment variables for the index setup
            export OPENSEARCH_ENDPOINT="https://$ENDPOINT"
            export AWS_REGION="$REGION"
            export VECTOR_INDEX_NAME="knowledge-embeddings"
            export EMBEDDING_DIMENSION="384"
            export SERVICE_ACCOUNT_ROLE_ARN="$SERVICE_ACCOUNT_ROLE_ARN"
            
            # Run the index setup script
            if python3 setup_opensearch_index.py; then
                echo "✅ OpenSearch index created successfully!"
            else
                echo "⚠️  OpenSearch index setup failed, but you can run it manually later:"
                echo "   export OPENSEARCH_ENDPOINT=https://$ENDPOINT"
                echo "   export AWS_REGION=$REGION"
                echo "   export SERVICE_ACCOUNT_ROLE_ARN=$SERVICE_ACCOUNT_ROLE_ARN"
                echo "   python3 setup_opensearch_index.py"
            fi
        else
            echo "⚠️  setup_opensearch_index.py not found, skipping index creation"
            echo "   You can create the index manually later"
        fi
    else
        echo "⚠️  OpenSearch cluster is still processing after 30 minutes"
        echo "   You can check the status in the AWS console and run index setup later"
    fi
    
    echo ""
    echo "📝 Next steps:"
    echo "1. Create the Kubernetes service account if it doesn't exist:"
    echo "   kubectl create serviceaccount strandsdk-rag-service-account -n $NAMESPACE"
    echo ""
    echo "2. Update your .env file with:"
    echo "   OPENSEARCH_ENDPOINT=https://$ENDPOINT"
    echo "   AWS_REGION=$REGION"
    echo ""
    echo "3. If index setup failed, run manually:"
    echo "   export OPENSEARCH_ENDPOINT=https://$ENDPOINT"
    echo "   export AWS_REGION=$REGION"
    SERVICE_ACCOUNT_ROLE_ARN=$(aws cloudformation describe-stacks \
        --stack-name $STACK_NAME \
        --region $REGION \
        --query 'Stacks[0].Outputs[?OutputKey==`ServiceAccountRoleArn`].OutputValue' \
        --output text 2>/dev/null || echo "")
    if [ -n "$SERVICE_ACCOUNT_ROLE_ARN" ]; then
        echo "   export SERVICE_ACCOUNT_ROLE_ARN=$SERVICE_ACCOUNT_ROLE_ARN"
    fi
    echo "   python3 setup_opensearch_index.py"
    
else
    echo "❌ Stack deployment failed"
    exit 1
fi
